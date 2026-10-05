"""L'API de la passe modèle : la seule qui engage de l'argent.

Elle doit donc pouvoir annoncer un coût sans rien dépenser, refuser d'agir sans
confirmation explicite, et ne jamais redemander ce qui a déjà été payé.
"""

import pytest
from conftest import make_candidate
from fakes import FakeDiscogs, FakeJudge, FakePlaylistClient

from ytmgc import verdicts
from ytmgc.models import Track
from ytmgc.verdicts import Verdict, VerdictBook
from ytmgc.web.app import Services, create_app
from ytmgc.web.jobs import JobRunner

fastapi_testclient = pytest.importorskip("fastapi.testclient")

CATALOGUE = {"Nirvana": [make_candidate(1, "Nevermind", "Nirvana", ("Rock",), ("Grunge",))]}
LIBRARY = [
    Track("g1", "Something In The Way", ("Nirvana",), "Nevermind"),
    Track("g2", "Lithium", ("Nirvana",), "Nevermind"),
]
ANSWERS = {
    "Something In The Way": Verdict(
        "Nirvana", "Something In The Way", "Rock", "Acoustic", "Mélancolique", 0.9,
        note="Berceuse sépulcrale.",
    ),
    "Lithium": Verdict("Nirvana", "Lithium", "Rock", "Grunge", "Énergique", 0.85),
}


class ScanningClient(FakePlaylistClient):
    def scan(self, sources):
        return list(LIBRARY)

    def list_playlist_summaries(self):
        return []


def build(repository, config, judge):
    services = Services(
        config=config,
        repository=repository,
        youtube_factory=lambda _c: ScanningClient(),
        discogs_factory=lambda _c: FakeDiscogs(CATALOGUE),
        judge_factory=lambda _c: judge,
        jobs=JobRunner(),
    )
    return fastapi_testclient.TestClient(create_app(services))


@pytest.fixture
def judge():
    return FakeJudge(ANSWERS)


@pytest.fixture
def client(repository, config, judge):
    repository.upsert_tracks(LIBRARY)
    return build(repository, config, judge)


def wait(client, response):
    job_id = response.json()["id"]
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "en cours":
            return job
    raise AssertionError("Le traitement ne se termine pas")


# ---------------------------------------------------------------- annonce


def scopes(state) -> dict:
    return {scope["key"]: scope for scope in state["scopes"]}


def test_the_cost_is_announced_without_spending_anything(client, judge):
    state = client.get("/api/enrich/state").json()

    assert state["available"] is True
    assert state["pending_tracks"] == 2
    assert scopes(state)["all"]["dollars"] > 0
    assert "$" in scopes(state)["all"]["line"]
    assert judge.submitted == []


def test_each_scope_prices_exactly_what_it_would_send(client, config):
    """Le montant affiché doit être celui de ce qui partira : c'est sur lui que
    l'utilisateur dit oui."""
    config.claude.pilot_size = 1
    by_key = scopes(client.get("/api/enrich/state").json())

    assert by_key["pilot"]["tracks"] == 1
    assert by_key["pilot"]["request"] == {"limit": 1, "only_unsorted": False}
    assert by_key["all"]["tracks"] == 2
    assert by_key["pilot"]["dollars"] < by_key["all"]["dollars"]


def test_the_pilot_never_offers_more_than_there_is_to_judge(client, config):
    config.claude.pilot_size = 500
    assert scopes(client.get("/api/enrich/state").json())["pilot"]["tracks"] == 2


def test_a_pilot_judges_only_what_was_asked(client, config, judge):
    config.claude.pilot_size = 1
    job = wait(client, client.post("/api/enrich", json={"confirm": True, "limit": 1}))

    assert job["result"]["collected"] == 1
    assert len(verdicts.load(config.claude.verdicts_file)) == 1
    # Le reste attend : un second passage le proposera, sans redemander le
    # titre déjà payé.
    assert client.get("/api/enrich/state").json()["pending_tracks"] == 1


def test_already_judged_tracks_drop_out_of_the_estimate(client, config):
    verdicts.save(VerdictBook([ANSWERS["Lithium"]]), config.claude.verdicts_file)
    state = client.get("/api/enrich/state").json()

    assert state["pending_tracks"] == 1
    assert state["verdicts"] == 1


def test_without_a_key_the_pass_is_announced_as_unavailable(repository, config):
    repository.upsert_tracks(LIBRARY)
    client = build(repository, config, None)

    assert client.get("/api/enrich/state").json()["available"] is False


# ------------------------------------------------------------- garde-fous


def test_the_pass_refuses_to_run_without_confirmation(client, judge):
    response = client.post("/api/enrich", json={"confirm": False})

    assert response.status_code == 400
    assert judge.submitted == []


def test_without_a_key_the_pass_explains_what_to_do(repository, config):
    repository.upsert_tracks(LIBRARY)
    client = build(repository, config, None)
    response = client.post("/api/enrich", json={"confirm": True})

    assert response.status_code == 400
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


# ----------------------------------------------------------------- passe


def test_the_pass_judges_writes_and_sorts(client, config, judge):
    job = wait(client, client.post("/api/enrich", json={"confirm": True}))

    assert job["status"] == "terminé", job["error"]
    assert job["result"]["collected"] == 2
    assert job["result"]["applied"] == 2
    # Le résultat payé est sur le disque, pas seulement en base.
    assert len(verdicts.load(config.claude.verdicts_file)) == 2


def test_the_verdict_splits_an_album_that_discogs_kept_together(repository, config):
    """Discogs range les quatre titres du même disque sous « Grunge ». Le
    modèle sait que deux d'entre eux sont des ballades : c'est exactement le
    défaut que cette passe est censée corriger."""
    album = [
        Track("g1", "Something In The Way", ("Nirvana",), "Nevermind"),
        Track("g2", "Polly", ("Nirvana",), "Nevermind"),
        Track("g3", "Lithium", ("Nirvana",), "Nevermind"),
        Track("g4", "Breed", ("Nirvana",), "Nevermind"),
    ]
    repository.upsert_tracks(album)
    answers = {
        "Something In The Way": Verdict(
            "Nirvana", "Something In The Way", "Rock", "Acoustic", "Mélancolique", 0.9),
        "Polly": Verdict("Nirvana", "Polly", "Rock", "Acoustic", "Mélancolique", 0.9),
        "Lithium": Verdict("Nirvana", "Lithium", "Rock", "Grunge", "Énergique", 0.9),
        "Breed": Verdict("Nirvana", "Breed", "Rock", "Grunge", "Énergique", 0.9),
    }
    client = build(repository, config, FakeJudge(answers))
    wait(client, client.post("/api/enrich", json={"confirm": True}))

    preview = client.post("/api/preview", json={"sort_mode": "exhaustif"}).json()["preview"]
    names = {playlist["name"] for playlist in preview["playlists"]}
    assert "Rock — Acoustic" in names
    assert "Rock — Grunge" in names


def test_a_second_pass_costs_nothing_more(client, judge):
    wait(client, client.post("/api/enrich", json={"confirm": True}))
    submitted = len(judge.submitted)
    job = wait(client, client.post("/api/enrich", json={"confirm": True}))

    assert len(judge.submitted) == submitted
    assert job["result"]["collected"] == 0


def test_an_unfinished_batch_is_resumed_rather_than_resubmitted(repository, config):
    """Un lot déposé est du travail payé : le redéposer le ferait payer deux fois."""
    repository.upsert_tracks(LIBRARY)
    slow = FakeJudge(ANSWERS, states=["in_progress"])
    client = build(repository, config, slow)

    from ytmgc.enrich import Pending, save_pending
    from ytmgc.sources.claude import Subject

    save_pending(
        Pending("batch-déjà-déposé", config.claude.model,
                [[Subject.of(track) for track in LIBRARY]]),
        config.claude.pending_file,
    )

    state = client.get("/api/enrich/state").json()
    assert state["batch"]["id"] == "batch-déjà-déposé"

    fast = FakeJudge(ANSWERS)
    client = build(repository, config, fast)
    job = wait(client, client.post("/api/enrich", json={"confirm": True}))

    assert fast.submitted == []
    assert job["result"]["collected"] == 2


# --------------------------------------------------------------- recherche


def test_a_track_can_be_looked_up_on_demand(client, config):
    response = client.post(
        "/api/lookup", json={"artist": "Nirvana", "title": "Something In The Way"}
    )
    payload = response.json()

    assert payload["cached"] is False
    assert payload["verdict"]["style"] == "Acoustic"
    assert payload["verdict"]["mood"] == "Mélancolique"
    assert payload["verdict"]["note"] == "Berceuse sépulcrale."
    # Le titre cherché rejoint le fichier : la prochaine analyse le reprendra.
    assert len(verdicts.load(config.claude.verdicts_file)) == 1


def test_looking_up_a_known_track_costs_nothing(client, config, judge):
    verdicts.save(VerdictBook([ANSWERS["Lithium"]]), config.claude.verdicts_file)
    payload = client.post(
        "/api/lookup", json={"artist": "Nirvana", "title": "Lithium"}
    ).json()

    assert payload["cached"] is True
    assert judge.polls == 0


def test_a_lookup_carries_the_definitions_so_the_answer_can_be_read(client):
    payload = client.post(
        "/api/lookup", json={"artist": "Nirvana", "title": "Something In The Way"}
    ).json()

    assert payload["verdict"]["mood_text"]
    assert payload["verdict"]["genre_text"]


def test_an_unknown_track_is_reported_rather_than_invented(client):
    response = client.post("/api/lookup", json={"artist": "Personne", "title": "Rien"})

    assert response.status_code == 502


def test_a_lookup_needs_both_fields(client):
    assert client.post("/api/lookup", json={"artist": "", "title": "Rien"}).status_code == 422


# ------------------------------------------------ passe gratuite (Claude.ai)


def test_the_library_is_exported_and_each_file_downloadable(client):
    payload = client.post("/api/chat/export", json={}).json()

    assert [f["name"] for f in payload["files"]] == ["titres-1-sur-1.txt"]
    assert payload["files"][0]["tracks"] == 2
    download = client.get(payload["files"][0]["url"])
    assert download.status_code == 200
    assert "Nirvana | Lithium" in download.text


def test_no_other_file_can_be_fetched(client):
    client.post("/api/chat/export", json={})
    assert client.get("/api/chat/files/export.json").status_code == 404
    assert client.get("/api/chat/files/..%2Fverdicts.txt").status_code == 404


def test_a_pasted_answer_is_imported_and_sorts_the_library(client, config):
    client.post("/api/chat/export", json={})
    text = "```\nNirvana | Lithium | Rock | Grunge | Énergique | 0.9 | Hymne.\n```"
    result = client.post("/api/chat/import", json={"text": text}).json()

    assert (result["accepted"], result["judged"], result["total"]) == (1, 1, 2)
    assert result["files"][0]["judged"] == 1
    assert len(verdicts.load(config.claude.verdicts_file)) == 1


def test_a_wrong_answer_is_refused_with_its_reason(client):
    response = client.post("/api/chat/import", json={"text": "pas de lignes ici"})

    assert response.status_code == 400
    assert "Copier" in response.json()["detail"]


def test_the_free_route_needs_no_api_key(repository, config):
    repository.upsert_tracks(LIBRARY)
    client = build(repository, config, None)

    assert client.post("/api/chat/export", json={}).json()["files"][0]["tracks"] == 2


def test_results_show_where_they_live_and_what_is_left(client, config):
    verdicts.save(VerdictBook([ANSWERS["Lithium"]]), config.claude.verdicts_file)
    payload = client.get("/api/verdicts").json()

    assert (payload["total"], payload["judged"], payload["todo"]) == (2, 1, 1)
    assert payload["file"].endswith("verdicts.txt")
    lithium = next(row for row in payload["rows"] if row["title"] == "Lithium")
    assert (lithium["judged"], lithium["genre"], lithium["mood"]) == (True, "Rock", "Énergique")


def test_results_can_be_filtered_and_searched(client, config):
    verdicts.save(VerdictBook([ANSWERS["Lithium"]]), config.claude.verdicts_file)

    todo = client.get("/api/verdicts", params={"show": "todo"}).json()
    assert [row["title"] for row in todo["rows"]] == ["Something In The Way"]
    found = client.get("/api/verdicts", params={"q": "lith"}).json()
    assert [row["title"] for row in found["rows"]] == ["Lithium"]
    assert client.get("/api/verdicts", params={"show": "n'importe"}).status_code == 400


def test_an_analysis_succeeds_when_verdicts_already_exist(client, config, repository):
    """Régression, par le chemin même où elle a été vue : relancer l'analyse
    après un import échouait — « cannot import name '_merge' »."""
    verdicts.save(VerdictBook([ANSWERS["Something In The Way"]]), config.claude.verdicts_file)

    job = wait(client, client.post("/api/analyse", json={"sources": ["library"]}))

    assert job["status"] == "terminé", job["error"]
    assert "tranchés par un verdict déjà rendu" in job["result"]["summary"]
    judged = {c.video_id: c for c in repository.classifications() if c.judged}
    assert [c.mood for c in judged.values()] == ["Mélancolique"]
