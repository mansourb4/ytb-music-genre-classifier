"""Relecture par playlist : Claude.ai signale les intrus, on tranche.

Ce qui compte : une réponse se rattache aux bons titres et aux bonnes
playlists, ce qui a été décidé à la main n'est jamais proposé, et rien ne
bouge sans un clic.
"""

import pytest
from fakes import FakeDiscogs, FakePlaylistClient

from ytmgc import placements, playlist_review, verdicts
from ytmgc.chat import ChatError
from ytmgc.classifier import classify_tracks
from ytmgc.models import Track
from ytmgc.placements import NOWHERE, Placement, PlacementBook
from ytmgc.playlist_plan import parse_plan
from ytmgc.playlist_review import FIXED, Proposal, ProposalBook
from ytmgc.preview import build_preview
from ytmgc.sorting import apply_sort_mode
from ytmgc.taxonomy import load_taxonomy
from ytmgc.verdicts import Verdict, VerdictBook

fastapi_testclient = pytest.importorskip("fastapi.testclient")

TAXONOMY = load_taxonomy()

PLAN = """
[[playlist]]
nom = "Jazz · Fusion"
description = "Fusion électrique des années 70."
styles = ["Fusion"]

[[playlist]]
nom = "Jazz · Jazz-funk"
styles = ["Jazz-Funk"]

[[playlist]]
nom = "Funk · Latin funk"
manuelle = true
"""

CHAMELEON = Track("j1", "Chameleon", ("Herbie Hancock",), "Head Hunters")
WATERMELON = Track("j2", "Watermelon Man", ("Herbie Hancock",))
BIRDLAND = Track("w1", "Birdland", ("Weather Report",), "Heavy Weather")

ANSWER = """```
FICHIER 1 SUR 1
Herbie Hancock | Chameleon | Jazz · Fusion | longue jam électrique
```"""


@pytest.fixture
def plan():
    return parse_plan(PLAN, TAXONOMY)


@pytest.fixture
def library(repository, config, tmp_path):
    path = tmp_path / "playlists.toml"
    path.write_text(PLAN, encoding="utf-8")
    config.taxonomy.playlists_file = str(path)
    tracks = [CHAMELEON, WATERMELON, BIRDLAND]
    repository.upsert_tracks(tracks)
    classify_tracks(tracks, repository, FakeDiscogs({}), config)
    verdicts.save(VerdictBook([
        Verdict("Herbie Hancock", "Chameleon", "Jazz", "Jazz-Funk", "Groovy", 0.9),
        Verdict("Herbie Hancock", "Watermelon Man", "Jazz", "Jazz-Funk", "Groovy", 0.9),
        Verdict("Weather Report", "Birdland", "Jazz", "Fusion", "Groovy", 0.9),
    ]), config.claude.verdicts_file)
    return repository


def preview(repository, config):
    summary, plans, _ = build_preview(repository, apply_sort_mode(config, "familles"),
                                      sort_mode="familles")
    return summary, plans


def read(text, plan, located=None, fixed=frozenset()):
    tracks = [CHAMELEON, WATERMELON, BIRDLAND]
    where = located or {
        key(CHAMELEON): "plan/jazz-jazz-funk",
        key(WATERMELON): "plan/jazz-jazz-funk",
        key(BIRDLAND): "plan/jazz-fusion",
    }
    return playlist_review.read_answer(text, tracks, plan, where, set(fixed))


def key(track):
    from ytmgc.verdicts import track_key

    return track_key(track)


# ---------------------------------------------------------------- lecture


def test_an_answer_becomes_proposals(plan):
    reading = read(ANSWER, plan)
    assert reading.proposals == [
        Proposal("Herbie Hancock", "Chameleon", "Jazz · Fusion", "longue jam électrique")
    ]
    assert reading.part == 1


def test_playlist_names_are_matched_tolerantly(plan):
    """Sans accents ni casse, ou sans la famille quand le nom est unique."""
    reading = read("herbie hancock | chameleon | jazz · FUSION\n"
                   "Herbie Hancock | Watermelon Man | latin funk", plan)
    assert [p.playlist for p in reading.proposals] == ["Jazz · Fusion", "Funk · Latin funk"]


def test_a_playlist_outside_the_plan_is_rejected_with_its_name(plan):
    reading = read("Herbie Hancock | Chameleon | Jazz · Cool", plan)
    assert reading.proposals == []
    assert reading.rejected == ["Herbie Hancock – Chameleon : playlist « Jazz · Cool » absente du plan"]


def test_an_unknown_track_is_rejected(plan):
    reading = read("Miles Davis | So What | Jazz · Fusion", plan)
    assert "aucun morceau" in reading.rejected[0]


def test_a_track_decided_by_hand_is_never_proposed(plan):
    reading = read(ANSWER, plan, fixed={key(CHAMELEON)})
    assert reading.proposals == []
    assert "déjà placé à la main" in reading.rejected[0]


def test_a_track_already_there_is_counted_not_proposed(plan):
    reading = read("Weather Report | Birdland | Jazz · Fusion", plan)
    assert reading.proposals == [] and reading.in_place == 1


def test_nowhere_can_be_proposed(plan):
    reading = read("Herbie Hancock | Chameleon | (aucune) | un sketch", plan)
    assert reading.proposals[0].playlist == NOWHERE
    assert reading.proposals[0].target is None


def test_nothing_to_report_is_a_valid_answer(plan):
    reading = read("```\nFICHIER 2 SUR 3\nRIEN\n```", plan)
    assert reading.proposals == [] and reading.part == 2


def test_an_unreadable_answer_says_what_to_do(plan):
    with pytest.raises(ChatError, match="artiste | titre | playlist proposée"):
        read("Voici mes remarques : tout est très bien.", plan)


# ------------------------------------------------------------------ export


class _Listed:
    def __init__(self, name, n):
        self.name = name
        self.tracks = [None] * n


def test_files_never_split_a_playlist_and_keep_families_together():
    playlists = [_Listed("Rap · US", 300), _Listed("Jazz · Fusion", 200),
                 _Listed("Rap · UK", 250), _Listed("Jazz · Bop", 350), _Listed("Divers", 700)]
    files = playlist_review.pack(playlists, size=600)
    assert [[p.name for p in f] for f in files] == [
        ["Divers"], ["Jazz · Bop", "Jazz · Fusion"], ["Rap · UK", "Rap · US"],
    ]


def test_the_export_lists_each_playlist_and_marks_decided_tracks(library, config, plan):
    placements.save(PlacementBook([Placement("Weather Report", "Birdland", "Jazz · Fusion")]),
                    config.taxonomy.placements_file)
    summary, _ = preview(library, config)
    fixed = playlist_review.fixed_keys(verdicts.load(config.claude.verdicts_file),
                                       placements.load(config.taxonomy.placements_file))
    files = playlist_review.export_review(summary, plan, config, fixed)

    assert [f.name for f in files] == ["playlists-1-sur-1.txt"]
    text = (playlist_review.directory(config) / files[0].name).read_text(encoding="utf-8")
    assert text.startswith("FICHIER 1 SUR 1 — 2 playlists, 3 titres à relire")
    assert "- Funk · Latin funk" in text  # proposée même vide
    assert "### Jazz · Fusion — 1 titres\nFusion électrique des années 70." in text
    assert f"Weather Report | Birdland | Fusion · Groovy {FIXED}" in text
    assert "Herbie Hancock | Chameleon | Jazz-Funk · Groovy\n" in text
    assert playlist_review.export_state(config)[0]["answered"] is False


def test_file_names_from_a_url_are_validated(config):
    assert playlist_review.export_path(config, "../../config.toml") is None


# ------------------------------------------------------------------ aperçu


def test_pending_proposals_are_shown_in_the_preview(library, config):
    playlist_review.save(ProposalBook([Proposal("Herbie Hancock", "Chameleon", "Jazz · Fusion", "jam")]),
                         config.taxonomy.proposals_file)
    summary, _ = preview(library, config)
    assert summary.proposals == [{
        "video_id": "j1", "title": "Chameleon", "artist": "Herbie Hancock", "thumbnail": None,
        "from_key": "plan/jazz-jazz-funk", "from_name": "Jazz · Jazz-funk",
        "to_key": "plan/jazz-fusion", "to_name": "Jazz · Fusion", "reason": "jam",
    }]


def test_a_proposal_decided_otherwise_is_no_longer_shown(library, config):
    playlist_review.save(ProposalBook([
        Proposal("Herbie Hancock", "Chameleon", "Jazz · Fusion"),
        Proposal("Herbie Hancock", "Watermelon Man", "Jazz · Ancienne"),
        Proposal("Weather Report", "Birdland", "Jazz · Fusion"),
    ]), config.taxonomy.proposals_file)
    placements.save(PlacementBook([Placement("Herbie Hancock", "Chameleon", "Funk · Latin funk")]),
                    config.taxonomy.placements_file)
    summary, _ = preview(library, config)
    # Déplacé à la main, playlist sortie du plan, déjà à sa place.
    assert summary.proposals == []


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


def test_export_import_then_accept(client, config):
    files = client.post("/api/review/export").json()["files"]
    assert [f["name"] for f in files] == ["playlists-1-sur-1.txt"]
    assert "Chameleon" in client.get(files[0]["url"]).text

    imported = client.post("/api/review/import", json={"text": ANSWER}).json()
    assert imported["proposed"] == 1 and imported["files"][0]["answered"] is True
    assert [p["video_id"] for p in imported["proposals"]] == ["j1"]

    accepted = client.post("/api/review/accept", json={"video_ids": ["j1"]}).json()
    assert accepted["placed"]["j1"]["playlist"] == "plan/jazz-fusion"
    assert [p.playlist for p in placements.load(config.taxonomy.placements_file)] == ["Jazz · Fusion"]
    assert len(playlist_review.load(config.taxonomy.proposals_file)) == 0


def test_refusing_keeps_the_track_and_validates_its_verdict(client, config):
    client.post("/api/review/import", json={"text": ANSWER})
    refused = client.post("/api/review/refuse", json={"video_ids": ["j1"]}).json()

    assert refused["placed"]["j1"]["playlist"] == "plan/jazz-jazz-funk"
    assert len(placements.load(config.taxonomy.placements_file)) == 0
    assert verdicts.load(config.claude.verdicts_file).for_track(CHAMELEON).source == verdicts.MANUAL
    assert len(playlist_review.load(config.taxonomy.proposals_file)) == 0


def test_moving_by_hand_settles_the_proposal(client, config):
    client.post("/api/review/import", json={"text": ANSWER})
    client.put("/api/placements", json={"video_id": "j1", "target": "plan/funk-latin-funk"})
    assert len(playlist_review.load(config.taxonomy.proposals_file)) == 0


def test_a_garbled_answer_is_a_400_with_guidance(client):
    response = client.post("/api/review/import", json={"text": "bonjour"})
    assert response.status_code == 400
    assert "Copier" in response.json()["detail"]


def test_accepting_without_a_proposal_is_a_404(client):
    assert client.post("/api/review/accept", json={"video_ids": ["j1"]}).status_code == 404
