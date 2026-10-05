"""Déplacements à la main : le dernier mot sur la place d'un morceau.

Ce qui compte : un déplacement tient — après une analyse, un changement de
règle, pour toutes les publications du morceau — et ne se perd jamais sans
un mot quand sa playlist disparaît du plan.
"""

import pytest
from fakes import FakeDiscogs, FakePlaylistClient

from ytmgc import cli, placements, verdicts
from ytmgc.classifier import classify_tracks
from ytmgc.models import Classification, MatchStatus, Track
from ytmgc.placements import NOWHERE, Placement, PlacementBook
from ytmgc.playlist_plan import parse_plan, plan_by_rules
from ytmgc.preview import build_preview
from ytmgc.sorting import apply_sort_mode
from ytmgc.taxonomy import load_taxonomy
from ytmgc.verdicts import Verdict, VerdictBook

fastapi_testclient = pytest.importorskip("fastapi.testclient")

TAXONOMY = load_taxonomy()

PLAN = """
[[playlist]]
nom = "Jazz · Fusion"
styles = ["Fusion"]

[[playlist]]
nom = "Jazz · Jazz-funk"
styles = ["Jazz-Funk"]

[[playlist]]
nom = "Électro · House"
genres = ["Electronic"]
"""

CHAMELEON = Track("j1", "Chameleon", ("Herbie Hancock",), "Head Hunters")
CHAMELEON_CLIP = Track("j2", "Chameleon (Remastered)", ("Herbie Hancock - Topic",))
BIRDLAND = Track("w1", "Birdland", ("Weather Report",), "Heavy Weather")
SKIT = Track("s1", "Intro (Skit)", ("Personne",))


def judged(video_id, genre, style):
    return Classification(video_id, MatchStatus.UNMATCHED, genres=(genre,), styles=(style,),
                          judged=True)


# ------------------------------------------------------------------ fichier


def test_the_file_round_trips(tmp_path):
    path = tmp_path / "placements.txt"
    book = PlacementBook()
    book.put(CHAMELEON, "Jazz · Jazz-funk")
    book.put(SKIT, NOWHERE)
    placements.save(book, path)

    again = placements.load(path)
    assert {p.title: p.playlist for p in again} == {"Chameleon": "Jazz · Jazz-funk",
                                                     "Intro (Skit)": NOWHERE}
    assert path.read_text(encoding="utf-8").startswith("# Déplacements ytmgc")


def test_a_hand_written_line_is_read_and_a_broken_one_skipped(tmp_path):
    path = tmp_path / "placements.txt"
    path.write_text("# commentaire\nHerbie Hancock\tChameleon\tJazz · Fusion\nbancale\n",
                    encoding="utf-8")
    assert [p.playlist for p in placements.load(path)] == ["Jazz · Fusion"]


def test_a_missing_file_means_no_move(tmp_path):
    assert len(placements.load(tmp_path / "absent.txt")) == 0


def test_every_publication_of_the_song_follows_the_move():
    """La chaîne « Topic » et le titre d'album sont un même morceau."""
    book = PlacementBook([Placement("Herbie Hancock", "Chameleon", "Jazz · Fusion")])
    resolved = placements.resolve([CHAMELEON, CHAMELEON_CLIP, BIRDLAND], book)
    assert set(resolved) == {"j1", "j2"}


def test_a_move_can_be_undone():
    book = PlacementBook()
    book.put(CHAMELEON, "Jazz · Fusion")
    assert book.remove(CHAMELEON) and len(book) == 0


# ----------------------------------------------------------------- planning


@pytest.fixture
def plan():
    return parse_plan(PLAN, TAXONOMY)


def names(plans):
    return {p.name: list(p.video_ids) for p in plans}


def test_a_move_beats_the_rules(plan):
    by_rules = names(plan_by_rules([judged("j1", "Jazz", "Jazz-Funk")], TAXONOMY, plan, "✱"))
    moved = names(plan_by_rules([judged("j1", "Jazz", "Jazz-Funk")], TAXONOMY, plan, "✱",
                                {"j1": "plan/jazz-fusion"}))

    assert by_rules == {"Jazz · Jazz-funk": ["j1"]}
    assert moved == {"Jazz · Fusion": ["j1"]}


def test_a_track_can_be_kept_out_of_every_playlist(plan):
    assert plan_by_rules([judged("j1", "Jazz", "Fusion")], TAXONOMY, plan, "✱", {"j1": None}) == []


def test_a_track_the_rules_cannot_place_can_still_be_moved(plan):
    """Le sketch n'entre dans aucune règle ; on doit pouvoir le ranger quand même."""
    skit = Classification("s1", MatchStatus.UNMATCHED)
    assert names(plan_by_rules([skit], TAXONOMY, plan, "✱", {"s1": "plan/electro-house"})) == {
        "Électro · House": ["s1"]}


def test_a_move_to_a_playlist_gone_from_the_plan_falls_back_to_the_rules(plan):
    plans = plan_by_rules([judged("j1", "Jazz", "Fusion")], TAXONOMY, plan, "✱",
                          {"j1": "plan/jazz-renommee"})
    assert names(plans) == {"Jazz · Fusion": ["j1"]}


def test_a_move_names_its_playlist_regardless_of_case_and_accents():
    assert Placement("A", "B", "electro · HOUSE").target == "plan/electro-house"


# ------------------------------------------------------------------- aperçu


@pytest.fixture
def library(repository, config, tmp_path):
    path = tmp_path / "playlists.toml"
    path.write_text(PLAN, encoding="utf-8")
    config.taxonomy.playlists_file = str(path)
    tracks = [CHAMELEON, BIRDLAND, SKIT]
    repository.upsert_tracks(tracks)
    classify_tracks(tracks, repository, FakeDiscogs({}), config)
    verdicts.save(VerdictBook([
        Verdict("Herbie Hancock", "Chameleon", "Jazz", "Jazz-Funk", "Groovy", 0.9),
        Verdict("Weather Report", "Birdland", "Jazz", "Fusion", "Groovy", 0.9),
    ]), config.claude.verdicts_file)
    return repository


def preview(repository, config):
    summary, _, _ = build_preview(repository, apply_sort_mode(config, "familles"),
                                  sort_mode="familles")
    return summary


def save(config, *entries):
    book = PlacementBook([Placement(*entry) for entry in entries])
    placements.save(book, config.taxonomy.placements_file)


def test_the_preview_offers_every_playlist_of_the_plan_as_a_target(library, config):
    targets = [t["name"] for t in preview(library, config).targets]
    assert targets == ["Jazz · Fusion", "Jazz · Jazz-funk", "Électro · House"]


def test_a_moved_track_is_shown_where_it_was_put(library, config):
    save(config, ("Herbie Hancock", "Chameleon", "Jazz · Fusion"))
    summary = preview(library, config)
    fusion = next(p for p in summary.playlists if p.name == "Jazz · Fusion")

    assert {t.title: t.moved for t in fusion.tracks} == {"Birdland": False, "Chameleon": True}
    assert summary.moved == 1
    assert all(p.name != "Jazz · Jazz-funk" for p in summary.playlists)


def test_a_track_kept_out_says_so(library, config):
    save(config, ("Weather Report", "Birdland", NOWHERE))
    left = {t["video_id"]: t["reason"] for t in preview(library, config).unsorted}
    assert left["w1"] == "kept_out"


def test_a_move_to_a_vanished_playlist_is_reported_not_lost(library, config):
    save(config, ("Herbie Hancock", "Chameleon", "Jazz · Ancienne"))
    summary = preview(library, config)

    assert summary.stale_placements == ["Jazz · Ancienne"]
    assert summary.moved == 0


def test_moves_only_apply_to_the_family_sort(library, config):
    save(config, ("Herbie Hancock", "Chameleon", NOWHERE))
    summary, _, _ = build_preview(library, apply_sort_mode(config, "detaille"),
                                  sort_mode="detaille")
    assert summary.targets == [] and summary.moved == 0


def test_the_cli_plan_honours_the_moves(library, config, monkeypatch, capsys):
    save(config, ("Personne", "Intro (Skit)", "Électro · House"))
    monkeypatch.setattr(cli, "_repository", lambda _config: library)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    from ytmgc.enrich import apply_verdicts

    apply_verdicts(library, verdicts.load(config.claude.verdicts_file), TAXONOMY, config)
    assert cli.main(["plan", "--tri", "familles"]) == 0
    assert "1  Électro · House" in capsys.readouterr().out


# ---------------------------------------------------------------------- web


@pytest.fixture
def client(library, config):
    from ytmgc.web.app import Services, create_app
    from ytmgc.web.jobs import JobRunner

    return fastapi_testclient.TestClient(create_app(Services(
        config=config, repository=library,
        youtube_factory=lambda _c: FakePlaylistClient(),
        discogs_factory=lambda _c: FakeDiscogs({}), jobs=JobRunner(),
    )))


def put(client, video_id, target):
    return client.put("/api/placements", json={"video_id": video_id, "target": target})


def test_moving_from_the_page_writes_the_file(client, config):
    response = put(client, "j1", "plan/jazz-fusion").json()

    assert response["playlist"] == "Jazz · Fusion"
    assert response["label"] == "Herbie Hancock – Chameleon"
    assert [p.playlist for p in placements.load(config.taxonomy.placements_file)] == ["Jazz · Fusion"]

    fusion = next(p for p in client.post("/api/preview", json={"sort_mode": "familles"})
                  .json()["preview"]["playlists"] if p["name"] == "Jazz · Fusion")
    assert fusion["count"] == 2


def test_a_track_can_be_given_back_to_the_rules(client, config):
    put(client, "j1", "plan/jazz-fusion")
    response = put(client, "j1", "regles").json()

    assert response["playlist"] is None
    assert len(placements.load(config.taxonomy.placements_file)) == 0


def test_a_track_can_be_kept_out_from_the_page(client, config):
    assert put(client, "w1", "aucune").json()["playlist"] == NOWHERE


def test_a_playlist_outside_the_plan_is_refused(client):
    response = put(client, "j1", "plan/inventee")
    assert response.status_code == 400 and "n'existe pas" in response.json()["detail"]


def test_an_unknown_track_is_refused(client):
    assert put(client, "nope", "aucune").status_code == 404


# --------------------------------------------------------------- relecture


def unsure_library(config):
    verdicts.save(VerdictBook([
        Verdict("Herbie Hancock", "Chameleon", "Jazz", "Jazz-Funk", "Groovy", 0.2, note="Peu sûr."),
        Verdict("Weather Report", "Birdland", "Jazz", "Fusion", "Groovy", 0.2),
    ]), config.claude.verdicts_file)


def unsure_titles(summary):
    return sorted(t.title for p in summary.playlists for t in p.tracks if t.unsure)


def test_unsure_tracks_are_counted_for_review(library, config):
    unsure_library(config)
    summary = preview(library, config)

    assert unsure_titles(summary) == ["Birdland", "Chameleon"]
    assert summary.to_review == 2
    track = next(t for p in summary.playlists for t in p.tracks if t.title == "Chameleon")
    assert track.main_artist == "Herbie Hancock"


def test_a_confirmed_verdict_leaves_the_review(client, library, config):
    unsure_library(config)
    response = client.post("/api/verdicts/confirm", json={"video_ids": ["j1", "j1", "nope"]}).json()

    assert response["confirmed"] == 1
    kept = verdicts.load(config.claude.verdicts_file)
    assert next(v for v in kept if v.title == "Chameleon").source == verdicts.MANUAL
    assert unsure_titles(preview(library, config)) == ["Birdland"]


def test_confirming_twice_changes_nothing(client, library, config):
    unsure_library(config)
    client.post("/api/verdicts/confirm", json={"video_ids": ["j1"]})
    assert client.post("/api/verdicts/confirm", json={"video_ids": ["j1"]}).json()["confirmed"] == 0


def test_a_moved_track_leaves_the_review(client, library, config):
    unsure_library(config)
    put(client, "w1", "plan/jazz-jazz-funk")
    assert unsure_titles(preview(library, config)) == ["Chameleon"]


def test_several_tracks_can_be_moved_at_once(client, config):
    response = client.put("/api/placements", json={
        "video_ids": ["j1", "w1"], "target": "plan/electro-house"}).json()

    assert response["count"] == 2 and response["label"] == "2 titres"
    assert {p.playlist for p in placements.load(config.taxonomy.placements_file)} == {"Électro · House"}


# ------------------------------------------------------------- suggestions


def suggestions_for(plans, profiles, plan_text):
    from ytmgc.preview import suggest_playlists

    return suggest_playlists(plans, profiles, parse_plan(plan_text, TAXONOMY))


def profile(artist, style, mood, genre="Jazz", countries=()):
    from ytmgc.preview import _Profile

    return _Profile(artist=artist, styles=frozenset({style}), mood=mood,
                    genres=frozenset({genre.casefold()}), countries=frozenset(countries))


def plan_of(**members):
    from ytmgc.models import PlaylistPlan

    from ytmgc.matching.normalize import slugify

    return [PlaylistPlan(key=f"plan/{slugify(k)}", name=k, description="", video_ids=tuple(v),
                         kind="plan")
            for k, v in members.items()]


SUGGEST_PLAN = """
[[playlist]]
nom = "a"
genres = ["Jazz"]
[[playlist]]
nom = "b"
genres = ["Jazz"]
[[playlist]]
nom = "c"
genres = ["Jazz"]
"""


def test_a_playlist_holding_the_same_artist_is_suggested_first():
    profiles = {
        "t": profile("herbie", "fusion", "Groovy"),
        "o": profile("herbie", "fusion", "Groovy"),
        "x": profile("autre", "fusion", "Groovy"),
    }
    got = suggestions_for(plan_of(a=["t"], b=["x"], c=["o"]), profiles, SUGGEST_PLAN)
    assert [s["name"] for s in got["t"]] == ["c", "b"]


def test_a_shared_mood_alone_is_not_enough():
    """L'ambiance seule rapprocherait Interstellar d'une transe gnaoua."""
    profiles = {
        "t": profile("zimmer", "minimalism", "Planant", genre="Classical"),
        "g": profile("guinia", "gnawa", "Planant", genre="Folk"),
    }
    got = suggestions_for(plan_of(a=["t"], b=["g"]), profiles, SUGGEST_PLAN)
    assert got["t"] == []


def test_a_known_country_is_respected():
    plan_text = ('[[playlist]]\nnom = "fr"\npays = ["FR"]\n'
                 '[[playlist]]\nnom = "ma"\npays = ["MA"]\n'
                 '[[playlist]]\nnom = "attente"\npays = ["?"]\n')
    profiles = {
        "t": profile("nekfeu", "trap", "Sombre", "Hip Hop", {"FR"}),
        "m": profile("toto", "trap", "Sombre", "Hip Hop", {"MA"}),
        "u": profile("inconnu", "trap", "Sombre", "Hip Hop"),
    }
    plans = plan_of(fr=["t"], ma=["m"], attente=["u"])
    got = suggestions_for(plans, profiles, plan_text)

    assert got["t"] == []
    assert [s["name"] for s in got["u"]] == ["fr", "ma"]


def test_the_preview_carries_suggestions_for_each_track(library, config):
    summary = preview(library, config)
    chameleon = next(t for p in summary.playlists for t in p.tracks if t.title == "Chameleon")
    assert [s["name"] for s in chameleon.suggestions] == ["Jazz · Fusion"]


def test_a_move_answers_with_the_new_place_of_each_track(client, library, config):
    unsure_library(config)
    placed = put(client, "j1", "plan/electro-house").json()["placed"]

    assert placed["j1"]["playlist"] == "plan/electro-house"
    track = placed["j1"]["track"]
    assert track["title"] == "Chameleon" and track["moved"] and not track["unsure"]


def test_a_track_kept_out_comes_back_as_unsorted(client):
    placed = put(client, "w1", "aucune").json()["placed"]
    assert placed["w1"] == {"playlist": None, "unsorted": placed["w1"]["unsorted"]}
    assert placed["w1"]["unsorted"]["reason"] == "kept_out"


def test_a_track_given_back_to_the_rules_returns_to_its_playlist(client):
    put(client, "j1", "plan/jazz-fusion")
    placed = put(client, "j1", "regles").json()["placed"]
    assert placed["j1"]["playlist"] == "plan/jazz-jazz-funk"
    assert not placed["j1"]["track"]["moved"]


def test_a_track_of_unknown_country_is_offered_one_playlist_per_country():
    """Pays inconnu : la question est le pays, pas l'ambiance. Trois variantes
    du rap français masqueraient le rap américain."""
    plan_text = ('[[playlist]]\nnom = "fr dur"\npays = ["FR"]\nambiances = ["Sombre"]\n'
                 '[[playlist]]\nnom = "fr doux"\npays = ["FR"]\n'
                 '[[playlist]]\nnom = "us"\npays = ["US"]\n'
                 '[[playlist]]\nnom = "attente"\npays = ["?"]\n')
    profiles = {
        "a": profile("a", "trap", "Sombre", "Hip Hop", {"FR"}),
        "b": profile("b", "trap", "Calme", "Hip Hop", {"FR"}),
        "c": profile("c", "trap", "Sombre", "Hip Hop", {"US"}),
        "u": profile("inconnu", "trap", "Sombre", "Hip Hop"),
    }
    plans = plan_of(**{"fr dur": ["a"], "fr doux": ["b"], "us": ["c"], "attente": ["u"]})
    got = suggestions_for(plans, profiles, plan_text)
    assert [s["name"] for s in got["u"]] == ["fr dur", "us"]
