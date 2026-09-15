"""La passe modèle : ce qui est payé doit être récupérable et reprenable."""

import json

import pytest
from fakes import FakeJudge

from ytmgc import verdicts
from ytmgc.enrich import (
    apply_verdicts,
    collect,
    load_pending,
    merge,
    pending_subjects,
    submit,
    wait,
)
from ytmgc.models import Classification, MatchStatus, Track
from ytmgc.taxonomy import load_taxonomy
from ytmgc.verdicts import Verdict, VerdictBook

LIBRARY = [
    Track("g1", "Something In The Way", ("Nirvana",), "Nevermind"),
    Track("g2", "Lithium", ("Nirvana",), "Nevermind"),
]
ANSWERS = {
    "Something In The Way": Verdict(
        "Nirvana", "Something In The Way", "Rock", "Acoustic", "Mélancolique", 0.9,
        note="Berceuse sépulcrale.",
    ),
    "Lithium": Verdict("Nirvana", "Lithium", "Rock", "Grunge", "Énergique", 0.8),
}


@pytest.fixture
def seeded(repository):
    repository.upsert_tracks(LIBRARY)
    for track in LIBRARY:
        repository.save_classification(
            Classification(track.video_id, MatchStatus.MATCHED, discogs_id=1, score=0.9,
                           genres=("Rock",), styles=("Grunge",))
        )
    return repository


def test_only_unjudged_tracks_are_submitted(seeded, config):
    book = VerdictBook([ANSWERS["Lithium"]])
    subjects = pending_subjects(seeded, book)

    assert [subject.title for subject in subjects] == ["Something In The Way"]


def test_the_submitted_subject_carries_what_the_databases_found(seeded, config):
    """Les métadonnées partent avec le titre : elles lèvent les homonymies,
    et le modèle a besoin de savoir ce qu'il contredit."""
    subject = pending_subjects(seeded, VerdictBook())[0]

    assert subject.album == "Nevermind"
    assert subject.genres == ("Rock",)
    assert "Discogs" in subject.line(1)


def test_a_submitted_batch_is_written_down_before_anything_else(seeded, config):
    """L'identifiant du lot est la seule trace d'un travail déjà payé : il doit
    atteindre le disque, sans quoi une coupure le perdrait."""
    judge = FakeJudge(ANSWERS)
    pending = submit(judge, pending_subjects(seeded, VerdictBook()), config)

    saved = load_pending(config.claude.pending_file)
    assert saved.batch_id == pending.batch_id
    assert saved.subjects == 2
    assert [s.title for s in saved.chunks[0]] == ["Something In The Way", "Lithium"]


def test_a_batch_is_split_according_to_the_configured_size(seeded, config):
    config.claude.batch_size = 1
    judge = FakeJudge(ANSWERS)
    pending = submit(judge, pending_subjects(seeded, VerdictBook()), config)

    assert len(pending.chunks) == 2
    assert len(judge.submitted[0]) == 2


def test_collecting_writes_the_verdicts_to_the_file(seeded, config):
    judge = FakeJudge(ANSWERS)
    pending = submit(judge, pending_subjects(seeded, VerdictBook()), config)
    book, stats = collect(judge, pending, config)

    assert stats.collected == 2
    assert len(verdicts.load(config.claude.verdicts_file)) == 2
    assert book.get(ANSWERS["Lithium"].key).style == "Grunge"


def test_a_collected_batch_is_forgotten(seeded, config):
    """Un lot récupéré ne doit plus figurer comme en attente, sans quoi la
    commande suivante irait rechercher des verdicts déjà écrits."""
    judge = FakeJudge(ANSWERS)
    pending = submit(judge, pending_subjects(seeded, VerdictBook()), config)
    collect(judge, pending, config)

    assert load_pending(config.claude.pending_file) is None


def test_a_partial_batch_keeps_what_succeeded(seeded, config):
    """Un lot à moitié réussi n'est pas une perte : les titres manquants
    repartiront seuls à la passe suivante."""
    judge = FakeJudge({"Lithium": ANSWERS["Lithium"]}, problems=["lot-0 : errored"])
    pending = submit(judge, pending_subjects(seeded, VerdictBook()), config)
    book, stats = collect(judge, pending, config)

    assert stats.collected == 1
    assert stats.problems == ["lot-0 : errored"]
    assert [s.title for s in pending_subjects(seeded, book)] == ["Something In The Way"]


def test_an_interrupted_run_resumes_from_the_file(seeded, config):
    """Le lot vit chez Anthropic : redémarrer l'outil ne doit rien coûter."""
    judge = FakeJudge(ANSWERS)
    submit(judge, pending_subjects(seeded, VerdictBook()), config)

    resumed = load_pending(config.claude.pending_file)
    book, stats = collect(judge, resumed, config)

    assert stats.collected == 2
    assert judge.submitted == judge.submitted[:1]  # rien n'a été redéposé


def test_an_unreadable_pending_file_is_ignored_rather_than_fatal(config):
    path = config.claude.pending_file
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ceci n'est pas du JSON")

    assert load_pending(path) is None


def test_waiting_polls_until_the_batch_ends():
    judge = FakeJudge(ANSWERS, states=["in_progress", "in_progress", "ended"])
    seen = []
    state = wait(
        judge, _pending(), on_status=lambda s, c: seen.append(s), sleep=lambda _s: None
    )

    assert state == "ended"
    assert seen == ["in_progress", "in_progress", "ended"]


def _pending():
    from ytmgc.enrich import Pending

    return Pending(batch_id="batch-1", model="claude-opus-5", chunks=[[]])


# --------------------------------------------------------- report en base


def test_a_verdict_overrides_what_discogs_said(seeded, config):
    book = VerdictBook(list(ANSWERS.values()))
    applied = apply_verdicts(seeded, book, load_taxonomy(), config)

    assert applied == 2
    by_id = {item.video_id: item for item in seeded.classifications()}
    assert by_id["g1"].styles == ("Acoustic",)
    assert by_id["g1"].mood == "Mélancolique"
    assert by_id["g1"].judged is True
    # Deux titres du même album ne partagent plus le même style : c'est tout
    # l'objet de la passe.
    assert by_id["g2"].styles == ("Grunge",)


def test_an_unsure_verdict_leaves_the_databases_alone(seeded, config):
    """Le modèle a dit qu'il ne connaissait pas le morceau : on le croit, et
    on garde ce que Discogs avait trouvé."""
    timid = Verdict("Nirvana", "Lithium", "Pop", "Bubblegum", "Festif", confidence=0.1)
    applied = apply_verdicts(seeded, VerdictBook([timid]), load_taxonomy(), config)

    assert applied == 0
    assert {c.video_id: c.styles for c in seeded.classifications()}["g2"] == ("Grunge",)


def test_a_verdict_makes_an_unmatched_track_sortable(repository, config):
    """Discogs n'a rien su dire, le modèle si : le titre doit cesser d'être
    invisible pour la planification."""
    repository.upsert_tracks([Track("x1", "Lithium", ("Nirvana",))])
    repository.save_classification(Classification("x1", MatchStatus.UNMATCHED))

    apply_verdicts(repository, VerdictBook([ANSWERS["Lithium"]]), load_taxonomy(), config)

    classification = repository.classifications()[0]
    assert classification.judged is True
    assert classification.status is MatchStatus.UNMATCHED  # `status` ne parle que de Discogs
    assert classification.styles == ("Grunge",)


def test_a_track_never_scanned_gets_a_classification_from_its_verdict(repository, config):
    repository.upsert_tracks([Track("x1", "Lithium", ("Nirvana",))])
    apply_verdicts(repository, VerdictBook([ANSWERS["Lithium"]]), load_taxonomy(), config)

    assert len(repository.classifications()) == 1


def test_the_model_spelling_is_normalised_to_the_project_one():
    """« Hip-Hop » et « Hip Hop » donneraient deux playlists pour un seul style."""
    taxonomy = load_taxonomy()
    verdict = Verdict("Nas", "N.Y. State of Mind", "Hip Hop", "Boom-Bap", "Sombre", 0.9)
    merged = merge(Track("v1", "N.Y. State of Mind", ("Nas",)), None, verdict, taxonomy)

    assert merged.styles == (taxonomy.canonical_style("Boom-Bap"),)


def test_applying_nothing_changes_nothing(seeded, config):
    assert apply_verdicts(seeded, VerdictBook(), load_taxonomy(), config) == 0


def test_a_second_application_is_idempotent(seeded, config):
    book = VerdictBook(list(ANSWERS.values()))
    apply_verdicts(seeded, book, load_taxonomy(), config)

    assert apply_verdicts(seeded, book, load_taxonomy(), config) == 0


def test_the_pending_file_holds_enough_to_rebuild_the_mapping(seeded, config):
    """Sans la découpe, un verdict ne saurait plus à quel titre il se rapporte."""
    submit(FakeJudge(ANSWERS), pending_subjects(seeded, VerdictBook()), config)
    raw = json.loads(open(config.claude.pending_file, encoding="utf-8").read())

    assert raw["chunks"][0][0]["title"] == "Something In The Way"
    assert raw["batch_id"]
