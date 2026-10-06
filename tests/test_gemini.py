"""Révision complète par Gemini : un prompt, une réponse, tout appliqué.

Ce qui compte : la réponse est lue même coupée en plusieurs messages, rien
d'invalide n'est appliqué, les décisions de l'utilisateur tiennent, et le
fichier du plan reste lisible — commentaires compris.
"""

import pytest
from fakes import FakeDiscogs

from ytmgc import cli, gemini, placements, playlist_review, verdicts
from ytmgc.classifier import classify_tracks
from ytmgc.gemini import Answer, Row
from ytmgc.models import Track
from ytmgc.placements import NOWHERE, Placement, PlacementBook
from ytmgc.playlist_plan import load_plan, parse_plan
from ytmgc.playlist_review import Proposal, ProposalBook
from ytmgc.preview import build_preview
from ytmgc.sorting import apply_sort_mode
from ytmgc.taxonomy import load_taxonomy
from ytmgc.verdicts import Verdict, VerdictBook

TAXONOMY = load_taxonomy()

PLAN = """\
# Le plan, avec ses commentaires.
[taille]
min = 1
max = 150

# ---- jazz
[[playlist]]
nom = "Jazz · Fusion"
description = "Fusion électrique."
styles = ["Fusion"]

[[playlist]]
nom = "Jazz · Jazz-funk"
styles = ["Jazz-Funk"]

[[playlist]]
nom = "Jazz · Jazz-funk"
artistes = ["Herbie Hancock"]

[[playlist]]
nom = "Électro · House"
genres = ["Electronic"]
"""

ROWS = {
    "T0001": Row("T0001", "Herbie Hancock", "Chameleon", "Jazz · Jazz-funk"),
    "T0002": Row("T0002", "Herbie Hancock", "Watermelon Man", "Jazz · Jazz-funk"),
    "T0003": Row("T0003", "Weather Report", "Birdland", "Jazz · Fusion"),
}
CURRENT = ["Jazz · Fusion", "Jazz · Jazz-funk", "Électro · House"]

ANSWER = """Voici ce que j'ai changé : la fusion devient plus précise.

```
PLAYLISTS
Jazz · Fusion électrique | Le jazz-rock des années 70.
Jazz · Jazz-funk | Groove et claviers.
Jazz · Dimanche matin | Doux et lumineux.

RENOMMAGES
Jazz · Fusion -> Jazz · Fusion électrique

DEPLACEMENTS
T0002 | Jazz · Dimanche matin
FIN
```"""


# ---------------------------------------------------------------- lecture


def test_an_answer_is_read_section_by_section():
    answer = gemini.parse(ANSWER)
    assert [n for n, _ in answer.playlists] == [
        "Jazz · Fusion électrique", "Jazz · Jazz-funk", "Jazz · Dimanche matin"]
    assert answer.renames == {"Jazz · Fusion": "Jazz · Fusion électrique"}
    assert answer.moves == {"T0002": "Jazz · Dimanche matin"}
    assert answer.finished


def test_an_answer_cut_in_several_messages_is_read_whole():
    first, second = ANSWER.split("T0002")
    answer = gemini.parse(first + "```\n\ncontinue\n\n```\nT0002" + second)
    assert answer.moves == {"T0002": "Jazz · Dimanche matin"}


def test_an_answer_without_playlists_is_refused():
    with pytest.raises(gemini.GeminiError, match="PLAYLISTS"):
        gemini.parse("Je n'ai rien changé.")


# ------------------------------------------------------------- changements


def test_renames_deletions_creations_and_moves_are_worked_out():
    change = gemini.plan_changes(gemini.parse(ANSWER), ROWS, CURRENT, set())
    assert change.renamed == {"Jazz · Fusion": "Jazz · Fusion électrique"}
    assert change.deleted == ["Électro · House"]
    assert change.created == [("Jazz · Dimanche matin", "Doux et lumineux.")]
    assert change.moves == {"T0002": "Jazz · Dimanche matin"}
    assert change.problems == []


def test_names_are_matched_without_accents_or_case():
    answer = Answer(playlists=[("Jazz · Fusion", ""), ("Jazz · Jazz-funk", ""),
                               ("Électro · House", "")],
                    moves={"T0001": "jazz · FUSION", "T0002": "(aucune)"})
    change = gemini.plan_changes(answer, ROWS, CURRENT, set())
    assert change.moves == {"T0001": "Jazz · Fusion", "T0002": NOWHERE}


def test_invalid_lines_are_reported_not_applied():
    answer = Answer(playlists=[(n, "") for n in CURRENT],
                    renames={"Jazz · Cool": "Jazz · Froid"},
                    moves={"T0001": "Jazz · Inexistant", "T9999": "Jazz · Fusion"})
    change = gemini.plan_changes(answer, ROWS, CURRENT, set())
    assert change.moves == {} and change.renamed == {}
    assert len(change.problems) == 3


def test_a_track_set_by_hand_stays_unless_its_playlist_goes():
    fixed = {ROWS["T0001"].key, ROWS["T0003"].key}
    answer = Answer(playlists=[("Jazz · Jazz-funk", ""), ("Électro · House", "")],
                    moves={"T0001": "Électro · House", "T0003": "Jazz · Jazz-funk"})
    change = gemini.plan_changes(answer, ROWS, CURRENT, fixed)
    # T0001 est fixé dans une playlist qui reste ; T0003 perd la sienne.
    assert change.kept_fixed == ["T0001"]
    assert change.moves == {"T0003": "Jazz · Jazz-funk"}


def test_a_forgotten_playlist_is_kept_not_deleted():
    """Une réponse coupée oublie des playlists entières : absente de la liste
    sans que ses titres soient replacés, une playlist est gardée."""
    answer = Answer(playlists=[("Jazz · Jazz-funk", ""), ("Électro · House", "")],
                    moves={"T0001": "Jazz · Fusion"})
    change = gemini.plan_changes(answer, ROWS, CURRENT, set())
    assert change.deleted == []
    assert any("gardée" in p and "Jazz · Fusion" in p for p in change.problems)
    # On peut encore y déplacer des titres.
    assert change.moves == {"T0001": "Jazz · Fusion"}


def test_a_playlist_is_deleted_once_all_its_tracks_have_a_place():
    answer = Answer(playlists=[("Jazz · Jazz-funk", ""), ("Électro · House", "")],
                    moves={"T0003": "Jazz · Jazz-funk"})
    change = gemini.plan_changes(answer, ROWS, CURRENT, set())
    assert change.deleted == ["Jazz · Fusion"]
    assert change.moves == {"T0003": "Jazz · Jazz-funk"}


# ------------------------------------------------------------------- plan


def test_the_plan_file_is_edited_block_by_block():
    change = gemini.plan_changes(gemini.parse(ANSWER), ROWS, CURRENT, set())
    text = gemini.edit_plan_text(PLAN, change)

    assert "# Le plan, avec ses commentaires." in text and "# ---- jazz" in text
    assert 'nom = "Jazz · Fusion électrique"' in text
    assert 'description = "Le jazz-rock des années 70."' in text
    assert "House" not in text
    assert text.count('nom = "Jazz · Jazz-funk"') == 2
    plan = parse_plan(text, TAXONOMY)
    names = [p.name for p in plan.playlists]
    assert names == ["Jazz · Fusion électrique", "Jazz · Jazz-funk", "Jazz · Dimanche matin"]
    assert plan.playlists[-1].rules == ()


# ------------------------------------------------------------ de bout en bout


@pytest.fixture
def library(repository, config, tmp_path):
    path = tmp_path / "playlists.toml"
    path.write_text(PLAN, encoding="utf-8")
    config.taxonomy.playlists_file = str(path)
    tracks = [Track("j1", "Chameleon", ("Herbie Hancock",)),
              Track("j2", "Watermelon Man", ("Herbie Hancock",)),
              Track("w1", "Birdland", ("Weather Report",))]
    repository.upsert_tracks(tracks)
    classify_tracks(tracks, repository, FakeDiscogs({}), config)
    verdicts.save(VerdictBook([
        Verdict("Herbie Hancock", "Chameleon", "Jazz", "Jazz-Funk", "Groovy", 0.9),
        Verdict("Herbie Hancock", "Watermelon Man", "Jazz", "Jazz-Funk", "Groovy", 0.9),
        Verdict("Weather Report", "Birdland", "Jazz", "Fusion", "Groovy", 0.9),
    ]), config.claude.verdicts_file)
    return repository


def preview(repository, config):
    summary, _, _ = build_preview(repository, apply_sort_mode(config, "familles"),
                                  sort_mode="familles")
    return summary


def test_the_prompt_numbers_every_track_and_marks_decisions(library, config):
    placements.save(PlacementBook([Placement("Weather Report", "Birdland", "Jazz · Fusion")]),
                    config.taxonomy.placements_file)
    summary = preview(library, config)
    plan = load_plan(config.taxonomy.playlists_file, TAXONOMY)
    fixed = playlist_review.fixed_keys(verdicts.load(config.claude.verdicts_file),
                                       placements.load(config.taxonomy.placements_file))
    prompt, rows = gemini.build(summary, plan, {}, fixed)

    assert [r.id for r in rows] == ["T0001", "T0002", "T0003"]
    assert "## Jazz · Fusion — 1 titres" in prompt
    assert "Description : Fusion électrique." in prompt
    assert "T0001 | Weather Report | Birdland | Jazz / Fusion · Groovy |  [fixé]" in prompt
    assert prompt.count("FORMAT DE LA RÉPONSE") == 2


def test_a_partial_prompt_lists_only_some_playlists(library, config):
    summary = preview(library, config)
    plan = load_plan(config.taxonomy.playlists_file, TAXONOMY)
    prompt, rows = gemini.build(summary, plan, {}, set(), only={"Jazz · Fusion"}, start=7)

    assert [r.id for r in rows] == ["T0007"]
    assert "## Jazz · Jazz-funk — 2 titres (déjà revue, titres non listés)" in prompt
    assert "Chameleon" not in prompt and "Birdland" in prompt
    assert "UNE PARTIE SEULEMENT" in prompt


def test_applying_writes_plan_and_moves_and_settles_proposals(library, config, tmp_path):
    playlist_review.save(ProposalBook([Proposal("Herbie Hancock", "Chameleon", "Jazz · Fusion")]),
                         config.taxonomy.proposals_file)
    summary = preview(library, config)
    plan = load_plan(config.taxonomy.playlists_file, TAXONOMY)
    _, rows = gemini.build(summary, plan, {}, set())
    by_title = {r.title: r.id for r in rows}
    answer = gemini.parse(ANSWER.replace("T0002", by_title["Watermelon Man"]))
    rows = {r.id: r for r in rows}

    dry = gemini.apply(answer, rows, config, TAXONOMY, write=False)
    assert dry.moves and "Dimanche" not in (tmp_path / "playlists.toml").read_text()

    gemini.apply(answer, rows, config, TAXONOMY, write=True)
    names = {p.name: [t.title for t in p.tracks] for p in preview(library, config).playlists}
    assert names == {
        "Jazz · Fusion électrique": ["Birdland"],
        "Jazz · Jazz-funk": ["Chameleon"],
        "Jazz · Dimanche matin": ["Watermelon Man"],
    }
    assert len(playlist_review.load(config.taxonomy.proposals_file)) == 0


def test_the_cli_checks_before_it_writes(library, config, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_repository", lambda _config: library)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    folder = tmp_path / "gemini"
    assert cli.main(["gemini-exporte", "--dossier", str(folder)]) == 0
    assert (folder / gemini.PROMPT_FILE).exists()
    answer = tmp_path / "reponse.txt"
    answer.write_text(ANSWER, encoding="utf-8")

    before = (tmp_path / "playlists.toml").read_text(encoding="utf-8")
    assert cli.main(["gemini-importe", str(answer), "--dossier", str(folder)]) == 0
    assert "Rien n'a été écrit" in capsys.readouterr().out
    assert (tmp_path / "playlists.toml").read_text(encoding="utf-8") == before

    assert cli.main(["gemini-importe", str(answer), "--dossier", str(folder), "--appliquer"]) == 0
    assert "Jazz · Fusion électrique" in (tmp_path / "playlists.toml").read_text(encoding="utf-8")
