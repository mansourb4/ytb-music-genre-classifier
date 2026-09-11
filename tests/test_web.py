"""Tests de l'API web, sans réseau ni serveur réel."""

import pytest
from conftest import make_candidate
from fakes import FakeDiscogs, FakePlaylistClient

from ytmgc.models import RemotePlaylist, Track
from ytmgc.web.app import Services, create_app
from ytmgc.web.jobs import JobRunner

fastapi_testclient = pytest.importorskip("fastapi.testclient")

CATALOGUE = {
    "Nirvana": [make_candidate(1, "Nevermind", "Nirvana", ("Rock",), ("Grunge",))],
    "Aphex Twin": [make_candidate(2, "SAW", "Aphex Twin", ("Electronic",), ("Ambient", "IDM"))],
}
LIBRARY = [Track(f"g{i}", f"Titre {i}", ("Nirvana",), "Nevermind") for i in range(5)]
LIBRARY += [Track(f"e{i}", f"Piste {i}", ("Aphex Twin",), "SAW") for i in range(5)]


class ScanningClient(FakePlaylistClient):
    """Client YouTube complet : lecture de bibliothèque et gestion de playlists."""

    def __init__(self, playlists=None, library=LIBRARY):
        super().__init__(playlists)
        self.library = library
        self.summaries = []
        self.scanned = []
        self.counted = []

    def scan(self, sources):
        self.scanned.append(list(sources))
        return list(self.library)

    def list_playlist_summaries(self):
        return list(self.summaries)

    def count_source(self, source):
        self.counted.append(source)
        if source == "library":
            return len(self.library)
        return {"liked": 3, "uploads": 0}.get(source, 42)


@pytest.fixture
def client(repository, config, tmp_path):
    config.youtube.auth_file = str(tmp_path / "browser.json")
    youtube = ScanningClient()
    services = Services(
        config=config,
        repository=repository,
        youtube_factory=lambda _config: youtube,
        discogs_factory=lambda _config: FakeDiscogs(CATALOGUE),
        jobs=JobRunner(),
    )
    test_client = fastapi_testclient.TestClient(create_app(services))
    test_client.youtube = youtube
    test_client.repository = repository
    return test_client


def wait(client, response):
    """Attend la fin du traitement lancé, puis renvoie son état final."""
    job_id = response.json()["id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "en cours":
            return job
    raise AssertionError("Le traitement ne se termine pas")


def analyse(client):
    job = wait(client, client.post("/api/analyse"))
    assert job["status"] == "terminé", job["error"]
    return job


# ------------------------------------------------------------------- lecture


def test_sort_modes_are_exposed_with_their_meaning(client):
    payload = client.get("/api/sort-modes").json()
    assert payload["default"] == "detaille"
    assert {mode["key"] for mode in payload["modes"]} >= {"detaille", "genre", "sans-doublon"}
    assert all(mode["summary"] and mode["detail"] for mode in payload["modes"])


def test_status_reports_an_empty_library(client):
    status = client.get("/api/status").json()
    assert status["tracks"] == 0 and status["classified"] == 0
    assert status["connected"] is False


def test_unknown_job_is_a_404(client):
    assert client.get("/api/jobs/inexistant").status_code == 404


# ------------------------------------------------------------------ analyse


def test_analyse_scans_then_classifies(client):
    analyse(client)
    status = client.get("/api/status").json()
    assert status["tracks"] == 10
    assert status["classified"] == 10
    assert status["unclassified"] == 0


def test_a_second_analysis_reuses_the_cache(client):
    analyse(client)
    job = analyse(client)
    assert "0 titre(s) traité(s)" in job["message"]


# ------------------------------------------------------------------- aperçu


def test_preview_lists_playlists_with_descriptions_and_tracks(client):
    analyse(client)
    payload = client.post("/api/preview", json={"sort_mode": "detaille"}).json()
    playlists = payload["preview"]["playlists"]

    assert {p["name"] for p in playlists} == {
        "Rock — Grunge", "Electronic — Ambient", "Electronic — IDM"
    }
    grunge = next(p for p in playlists if p["name"] == "Rock — Grunge")
    assert "GENRE — Rock" in grunge["description"]
    assert grunge["change"] == "création"
    assert len(grunge["tracks"]) == grunge["count"]
    assert {"title", "artist", "album", "thumbnail", "genres", "styles", "year"} <= set(
        grunge["tracks"][0]
    )
    assert payload["actions"]


def test_preview_changes_with_the_sort_mode(client):
    analyse(client)
    detailed = client.post("/api/preview", json={"sort_mode": "detaille"}).json()
    by_genre = client.post("/api/preview", json={"sort_mode": "genre"}).json()
    assert len(by_genre["preview"]["playlists"]) < len(detailed["preview"]["playlists"])


def test_preview_writes_nothing(client):
    analyse(client)
    client.post("/api/preview", json={"sort_mode": "detaille"})
    assert [c for c in client.youtube.calls if c != "list"] == []


def test_unknown_sort_mode_is_rejected(client):
    response = client.post("/api/preview", json={"sort_mode": "n-importe-quoi"})
    assert response.status_code == 400
    assert "Type de tri inconnu" in response.json()["detail"]


def test_preview_reports_updates_against_the_existing_state(client):
    analyse(client)
    plan = next(
        p for p in client.post("/api/preview", json={"sort_mode": "detaille"}).json()["preview"]["playlists"]
        if p["key"] == "rock/grunge"
    )
    client.youtube._playlists["PLx"] = RemotePlaylist(
        "PLx", plan["name"], plan["description"], ("g0", "intrus"), {"g0": "s0", "intrus": "s1"}
    )
    refreshed = client.post("/api/preview", json={"sort_mode": "detaille"}).json()
    grunge = next(p for p in refreshed["preview"]["playlists"] if p["key"] == "rock/grunge")
    assert grunge["change"] == "mise à jour"
    assert grunge["added"] == 4 and grunge["removed"] == 1


# -------------------------------------------------------------- application


def test_apply_requires_an_explicit_confirmation(client):
    analyse(client)
    response = client.post("/api/apply", json={"sort_mode": "detaille"})
    assert response.status_code == 400
    assert client.youtube.list_playlists() == []


def test_apply_creates_the_previewed_playlists(client):
    analyse(client)
    previewed = {
        p["name"] for p in client.post("/api/preview", json={"sort_mode": "detaille"}).json()["preview"]["playlists"]
    }
    job = wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))

    assert job["status"] == "terminé", job["error"]
    assert {p.title for p in client.youtube.list_playlists()} == previewed
    assert client.get("/api/status").json()["managed_playlists"] == len(previewed)


def test_apply_uses_the_requested_sort_mode(client):
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "genre", "confirm": True}))
    assert all("—" not in p.title or "Autres styles" in p.title or "Divers" in p.title
               for p in client.youtube.list_playlists())


def test_applying_twice_changes_nothing(client):
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    before = {p.playlist_id: p.video_ids for p in client.youtube.list_playlists()}
    job = wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    assert job["result"]["applied"] == 0
    assert {p.playlist_id: p.video_ids for p in client.youtube.list_playlists()} == before


# --------------------------------------------------------------- annulation


def test_purge_requires_an_explicit_confirmation(client):
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    assert client.post("/api/purge", json={}).status_code == 400
    assert client.youtube.list_playlists() != []


def test_purge_removes_generated_playlists_and_keeps_the_others(client):
    perso = RemotePlaylist("PLperso", "Ma sélection", "faite à la main", ("g0",))
    client.youtube._playlists["PLperso"] = perso
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))

    job = wait(client, client.post("/api/purge", json={"confirm": True}))

    assert job["status"] == "terminé", job["error"]
    assert client.youtube.list_playlists() == [perso]
    assert client.get("/api/status").json()["managed_playlists"] == 0


def test_purge_then_apply_restores_the_playlists(client):
    """L'annulation doit être réversible en relançant l'application."""
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    wait(client, client.post("/api/purge", json={"confirm": True}))
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    assert len(client.youtube.list_playlists()) == 3


# ------------------------------------------------------------------- divers


def test_only_one_job_runs_at_a_time(client, monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()

    def slow_scan(_sources):
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr(client.youtube, "scan", slow_scan)
    client.post("/api/analyse")
    started.wait(5)
    try:
        assert client.post("/api/analyse").status_code == 409
    finally:
        release.set()


def test_a_failing_job_reports_its_error(client, monkeypatch):
    def failing(_sources):
        raise RuntimeError("session expirée")

    monkeypatch.setattr(client.youtube, "scan", failing)
    job = wait(client, client.post("/api/analyse"))
    assert job["status"] == "échoué"
    assert "session expirée" in job["error"]


def test_the_interface_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Organiser sa bibliothèque YouTube Music" in response.text


# ------------------------------------------------------ connexion OAuth (web)


def test_connect_methods_reports_what_is_configured(client):
    payload = client.get("/api/connect/methods").json()
    assert payload["oauth_client_configured"] is False
    assert payload["connected"] is False


def test_oauth_start_returns_a_link_and_remembers_the_client(client, monkeypatch):
    """Aucun appel réseau : seule la mécanique de l'application est testée."""
    from ytmgc.sources import oauth

    monkeypatch.setattr(
        oauth, "start_device_flow",
        lambda _client, credentials=None: {
            "url": "https://google/device?user_code=ABC", "user_code": "ABC",
            "device_code": "dev", "interval": 5, "expires_in": 1800,
            "verification_url": "https://google/device",
        },
    )

    response = client.post(
        "/api/connect/oauth/start",
        json={"client_id": "123456789.apps.googleusercontent.com", "client_secret": "secret"},
    )
    assert response.status_code == 200
    assert response.json()["user_code"] == "ABC"
    assert client.get("/api/connect/methods").json()["oauth_client_configured"] is True


def test_oauth_start_rejects_incomplete_credentials(client):
    assert client.post("/api/connect/oauth/start", json={"client_id": "x", "client_secret": "y"}).status_code == 422


def test_oauth_poll_without_a_registered_client_is_refused(client):
    response = client.post("/api/connect/oauth/poll", json={"device_code": "dev"})
    assert response.status_code == 400
    assert "identifiant client" in response.json()["detail"].lower()


def test_pasted_headers_missing_the_cookie_are_refused_with_guidance(client):
    response = client.post("/api/connect", json={"headers": "accept: */*\nuser-agent: Mozilla"})
    assert response.status_code == 400
    assert "cookie" in response.json()["detail"]


def test_a_pasted_curl_command_connects_the_account(client):
    """Le geste identique dans tous les navigateurs : « Copier comme cURL »."""
    curl = (
        "curl 'https://music.youtube.com/youtubei/v1/browse' "
        "-H 'accept: */*' "
        "-H 'cookie: SID=xyz; __Secure-3PAPISID=signature'"
    )
    assert client.post("/api/connect", json={"headers": curl}).status_code == 200
    assert client.get("/api/status").json()["connected"] is True


# ------------------------------------------------------- choix des sources


def test_sources_list_offers_library_and_user_playlists(client):
    client.youtube.summaries = [
        {"playlist_id": "PL1", "title": "Road trip"},
        {"playlist_id": "PL2", "title": "Chill"},
    ]
    payload = client.get("/api/sources").json()

    assert [entry["key"] for entry in payload["special"]] == ["library", "liked", "uploads"]
    assert [entry["label"] for entry in payload["playlists"]] == ["Road trip", "Chill"]
    assert payload["defaults"] == ["library", "liked"]
    assert payload["reachable"] is True


def test_generated_playlists_are_not_offered_as_sources(client):
    """Les analyser reviendrait à reclasser sa propre sortie."""
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    created = client.youtube.list_playlists()
    client.youtube.summaries = [
        {"playlist_id": created[0].playlist_id, "title": created[0].title},
        {"playlist_id": "PLperso", "title": "Ma sélection"},
    ]

    payload = client.get("/api/sources").json()
    assert [entry["label"] for entry in payload["playlists"]] == ["Ma sélection"]


def test_sources_degrade_gracefully_when_the_account_is_unreachable(client, monkeypatch):
    def failing():
        raise RuntimeError("session expirée")

    monkeypatch.setattr(client.youtube, "list_playlist_summaries", failing)
    payload = client.get("/api/sources").json()
    assert payload["reachable"] is False
    assert payload["playlists"] == []
    assert payload["special"]


def test_analysis_reads_only_the_selected_sources(client):
    job = wait(client, client.post("/api/analyse", json={"sources": ["playlist:PL9"]}))
    assert job["status"] == "terminé", job["error"]
    assert job["result"]["sources"] == ["playlist:PL9"]
    assert client.youtube.scanned == [["playlist:PL9"]]


def test_an_unknown_source_is_refused_before_starting(client):
    response = client.post("/api/analyse", json={"sources": ["dossier"]})
    assert response.status_code == 400
    assert "Source inconnue" in response.json()["detail"]
    assert client.youtube.scanned == []


def test_an_empty_selection_is_refused(client):
    """Tout décocher ne doit pas retomber sur la configuration, sans quoi le
    geste produirait exactement l'inverse de ce qu'il exprime."""
    response = client.post("/api/analyse", json={"sources": []})
    assert response.status_code == 400
    assert "Aucune source" in response.json()["detail"]
    assert client.youtube.scanned == []


def test_omitting_the_field_still_uses_the_configured_sources(client):
    job = wait(client, client.post("/api/analyse"))
    assert job["result"]["sources"] == ["library", "liked"]


def test_a_job_reports_its_elapsed_time(client):
    job = analyse(client)
    assert job["elapsed_s"] >= 0


# --------------------------------------------------- sélection dans l'aperçu


def previewed(client, sort_mode="detaille"):
    return client.post("/api/preview", json={"sort_mode": sort_mode}).json()["preview"]["playlists"]


def test_each_analysis_starts_from_a_clean_slate(client):
    """L'aperçu doit refléter les sources cochées, pas cumuler les analyses."""
    analyse(client)
    assert client.get("/api/status").json()["tracks"] == 10

    client.youtube.library = [Track("z1", "Seul", ("Nirvana",), "Nevermind")]
    analyse(client)

    assert client.get("/api/status").json()["tracks"] == 1


def test_an_excluded_playlist_is_never_created(client):
    analyse(client)
    keys = [p["key"] for p in previewed(client)]
    excluded = keys[0]

    wait(client, client.post("/api/apply", json={
        "sort_mode": "detaille", "confirm": True, "excluded_playlists": [excluded],
    }))

    created = {p.title for p in client.youtube.list_playlists()}
    assert len(created) == len(keys) - 1


def test_an_excluded_playlist_that_exists_is_left_untouched(client):
    """Décocher signifie « n'y touche pas », pas « efface-la »."""
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    before = {p.playlist_id: p.video_ids for p in client.youtube.list_playlists()}
    target = previewed(client)[0]
    key, name = target["key"], target["name"]

    # Une seconde application, cette playlist écartée et la bibliothèque vidée.
    client.youtube.library = []
    analyse(client)
    wait(client, client.post("/api/apply", json={
        "sort_mode": "detaille", "confirm": True, "excluded_playlists": [key],
    }))

    after = {p.playlist_id: p.video_ids for p in client.youtube.list_playlists()}
    untouched = next(p for p in client.youtube.list_playlists() if p.title == name)
    assert after[untouched.playlist_id] == before[untouched.playlist_id]


def test_excluded_tracks_are_left_out_of_the_playlist(client):
    analyse(client)
    target = previewed(client)[0]
    dropped = target["tracks"][0]["video_id"]

    wait(client, client.post("/api/apply", json={
        "sort_mode": "detaille", "confirm": True,
        "excluded_tracks": {target["key"]: [dropped]},
    }))

    created = next(p for p in client.youtube.list_playlists() if p.title == target["name"])
    assert dropped not in created.video_ids
    assert len(created.video_ids) == target["count"] - 1


def test_excluding_every_track_drops_the_playlist(client):
    analyse(client)
    target = previewed(client)[0]

    wait(client, client.post("/api/apply", json={
        "sort_mode": "detaille", "confirm": True,
        "excluded_tracks": {target["key"]: [t["video_id"] for t in target["tracks"]]},
    }))

    assert target["name"] not in {p.title for p in client.youtube.list_playlists()}


def test_preview_exposes_the_description_in_parts(client):
    """L'interface doit pouvoir présenter la description autrement qu'en bloc brut."""
    analyse(client)
    grunge = next(p for p in previewed(client) if p["name"] == "Rock — Grunge")

    assert grunge["genre"] == "Rock" and grunge["style"] == "Grunge"
    assert "rock'n'roll" in grunge["genre_text"]
    assert "Seattle" in grunge["style_text"]
    assert grunge["note"] is None


# ---------------------------------------------- suppression choisie playlist


def generated(client):
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    return client.get("/api/purge/candidates").json()["playlists"]


def test_purge_candidates_list_what_was_detected(client):
    """La suppression étant irréversible, elle commence par montrer sa cible."""
    client.youtube._playlists["PLperso"] = RemotePlaylist("PLperso", "Ma sélection", "à moi", ("g0",))
    candidates = generated(client)

    assert "Ma sélection" not in {c["title"] for c in candidates}
    assert {"playlist_id", "title", "count", "thumbnail", "key"} <= set(candidates[0])
    assert candidates[0]["count"] > 0
    assert candidates == sorted(candidates, key=lambda c: c["title"])


def test_listing_candidates_deletes_nothing(client):
    before = len(generated(client))
    assert len(client.get("/api/purge/candidates").json()["playlists"]) == before


def test_only_the_chosen_playlists_are_deleted(client):
    candidates = generated(client)
    target = candidates[0]

    job = wait(client, client.post("/api/purge", json={
        "confirm": True, "playlist_ids": [target["playlist_id"]],
    }))

    assert job["result"]["deleted"] == 1
    remaining = {p.title for p in client.youtube.list_playlists()}
    assert target["title"] not in remaining
    assert len(remaining) == len(candidates) - 1


def test_an_unmanaged_playlist_is_never_deleted_even_if_asked(client):
    """Le marqueur reste le dernier mot, quelle que soit la demande."""
    perso = RemotePlaylist("PLperso", "Ma sélection", "à moi", ("g0",))
    client.youtube._playlists["PLperso"] = perso
    generated(client)

    job = wait(client, client.post("/api/purge", json={
        "confirm": True, "playlist_ids": ["PLperso"],
    }))

    assert job["result"]["deleted"] == 0
    assert perso in client.youtube.list_playlists()


def test_omitting_the_selection_still_deletes_everything(client):
    generated(client)
    wait(client, client.post("/api/purge", json={"confirm": True}))
    assert client.youtube.list_playlists() == []


def test_candidates_require_a_reachable_account(client, monkeypatch):
    def failing():
        raise RuntimeError("session expirée")

    monkeypatch.setattr(client.youtube, "list_playlists", failing)
    assert client.get("/api/purge/candidates").status_code == 400


# ------------------------------------------------------ décompte des sources


def test_each_source_can_be_counted(client):
    assert client.get("/api/sources/count?source=library").json() == {
        "source": "library", "count": 10
    }
    assert client.get("/api/sources/count?source=liked").json()["count"] == 3
    assert client.get("/api/sources/count?source=playlist:PL1").json()["count"] == 42


def test_a_count_is_measured_once_then_remembered(client):
    """Compter la bibliothèque suppose de la parcourir : à ne pas refaire à
    chaque affichage."""
    client.get("/api/sources/count?source=library")
    client.get("/api/sources/count?source=library")
    assert client.youtube.counted == ["library"]


def test_counting_an_unknown_source_is_refused(client):
    response = client.get("/api/sources/count?source=dossier")
    assert response.status_code == 400
    assert "Source inconnue" in response.json()["detail"]


def test_a_failing_count_is_reported_rather_than_guessed(client, monkeypatch):
    def failing(_source):
        raise RuntimeError("playlist supprimée")

    monkeypatch.setattr(client.youtube, "count_source", failing)
    response = client.get("/api/sources/count?source=liked")
    assert response.status_code == 400
    assert "playlist supprimée" in response.json()["detail"]


def test_the_unreliable_playlist_count_is_no_longer_exposed(client):
    """ytmusicapi construit `count` avec le premier mot d'un sous-titre : il
    vaut « 2 » pour « 2 188 titres », et un mot quelconque selon la langue."""
    client.youtube.summaries = [{"playlist_id": "PL1", "title": "Favorite Songs", "count": "2"}]
    playlists = client.get("/api/sources").json()["playlists"]
    assert "count" not in playlists[0]


def test_a_finished_job_never_reports_without_its_cause(client, monkeypatch):
    """Le statut final se publie après le résultat : un sondage tombant entre
    les deux montrerait sinon un échec sans cause."""
    def failing(_sources):
        raise RuntimeError("session expirée")

    monkeypatch.setattr(client.youtube, "scan", failing)
    for _ in range(20):
        job = wait(client, client.post("/api/analyse"))
        assert job["status"] == "échoué"
        assert job["error"], "un échec doit toujours porter son message"
