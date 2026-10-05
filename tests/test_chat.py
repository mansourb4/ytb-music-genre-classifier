"""La passe gratuite : la bibliothèque exportée en fichiers, jugée dans des
conversations Claude.ai, et les réponses recollées.

Rien ne contraint la forme d'une réponse de conversation. Le danger n'est pas
la ligne manquante — son morceau reste « à juger » et revient au prochain
export — mais la ligne rattachée au mauvais morceau, tenue ensuite pour
acquise. Chaque ligne porte donc elle-même l'identité de son morceau.
"""

import pytest

from ytmgc import verdicts
from ytmgc.chat import (
    ChatError,
    build_file,
    export_library,
    export_path,
    export_progress,
    import_answer,
    read_answer,
)
from ytmgc.models import Track
from ytmgc.sources.claude import SYSTEM, Subject
from ytmgc.taxonomy import load_taxonomy
from ytmgc.verdicts import Verdict, VerdictBook

TAXONOMY = load_taxonomy()
LIBRARY = [
    Track("g1", "Something In The Way", ("Nirvana",), "Nevermind"),
    Track("g2", "Get Lucky", ("Daft Punk", "Pharrell Williams"), "RAM"),
    Track("g3", "Creep", ("Radiohead",), "Pablo Honey"),
]

GOOD = """```
artiste | titre | genre | style | ambiance | confiance | note
Nirvana | Something In The Way | Rock | Acoustic | Mélancolique | 0.95 | Berceuse sépulcrale.
Daft Punk | Get Lucky | Electronic | Disco | Festif | 0.9 | Disco solaire.
Radiohead | Creep | Rock | Alternative Rock | Mélancolique | 0.9 | Ballade de l'inadapté.
```"""


@pytest.fixture
def library(repository):
    repository.upsert_tracks(LIBRARY)
    return repository


# ------------------------------------------------------------------ export


def test_a_file_carries_instructions_vocabulary_and_titles():
    text = build_file([Subject("Nirvana", "Something In The Way", "Nevermind", 1991)],
                      2, 9, TAXONOMY)

    assert text.startswith("FICHIER 2 SUR 9")
    assert "musicologue" in text
    assert "artiste | titre | genre | style | ambiance | confiance | note" in text
    assert "Mélancolique" in text and "Deep House" in text
    assert "Nirvana | Something In The Way | album « Nevermind » ; 1991" in text


def test_guests_are_hints_not_part_of_the_artist_to_copy():
    """La réponse doit recopier l'artiste principal : les invités vont dans
    les indices, sans quoi la ligne ne retrouverait pas son morceau."""
    text = build_file([Subject("Daft Punk", "Get Lucky", featuring=("Pharrell Williams",))],
                      1, 1, TAXONOMY)
    assert "Daft Punk | Get Lucky | avec Pharrell Williams" in text


def test_a_pipe_inside_a_title_cannot_break_the_columns():
    text = build_file([Subject("Artiste", "Avant | Après")], 1, 1, TAXONOMY)
    assert "Artiste | Avant / Après |" in text


def test_the_judging_rules_are_those_of_the_paid_route():
    """Mêmes règles des deux côtés, sans quoi les deux voies ne donneraient
    pas la même précision."""
    text = build_file([Subject("A", "B")], 1, 1, TAXONOMY)
    assert "Juge le morceau, jamais l'album" in text
    assert "Juge le morceau, jamais l'album" in SYSTEM


def test_the_whole_library_is_exported_at_once_in_parts(library, config):
    config.claude.export_size = 2
    files = export_library(library, config, TAXONOMY)

    assert [f.name for f in files] == ["titres-1-sur-2.txt", "titres-2-sur-2.txt"]
    assert sum(len(f.keys) for f in files) == 3
    assert all(f.path.exists() for f in files)


def test_judged_songs_are_left_out_of_the_export(library, config):
    verdicts.save(VerdictBook([Verdict("Radiohead", "Creep", "Rock", "Grunge", "Sombre", 0.9)]),
                  config.claude.verdicts_file)
    files = export_library(library, config, TAXONOMY)

    assert sum(len(f.keys) for f in files) == 2
    assert "Creep" not in files[0].path.read_text(encoding="utf-8").split("TITRES À JUGER")[1]


def test_a_new_export_replaces_the_previous_files(library, config):
    config.claude.export_size = 1
    export_library(library, config, TAXONOMY)
    config.claude.export_size = 10
    files = export_library(library, config, TAXONOMY)

    on_disk = sorted(p.name for p in files[0].path.parent.glob("titres-*.txt"))
    assert on_disk == ["titres-1-sur-1.txt"]


def test_a_fully_judged_library_exports_nothing(library, config):
    verdicts.save(
        VerdictBook([Verdict(t.artist, t.title, "Rock", "Grunge", "Calme", 0.9) for t in LIBRARY]),
        config.claude.verdicts_file,
    )
    assert export_library(library, config, TAXONOMY) == []


def test_only_exported_files_can_be_fetched_by_name(library, config):
    export_library(library, config, TAXONOMY)

    assert export_path(config, "titres-1-sur-1.txt") is not None
    assert export_path(config, "../verdicts.txt") is None
    assert export_path(config, "export.json") is None


# ----------------------------------------------------------------- lecture


def test_a_clean_answer_is_read_whole():
    reading = read_answer(GOOD, LIBRARY, TAXONOMY)

    assert [v.title for v in reading.verdicts] == ["Something In The Way", "Get Lucky", "Creep"]
    assert reading.verdicts[0].note == "Berceuse sépulcrale."
    assert reading.rejected == []


def test_a_line_naming_a_song_outside_the_library_is_set_aside():
    """Le cas dangereux : une ligne qui ne désigne aucun morceau connu ne doit
    s'écrire sous aucun."""
    reading = read_answer("Radiohead | Paranoid Android | Rock | Art Rock | Sombre | 0.9 | x",
                          LIBRARY, TAXONOMY)

    assert reading.verdicts == []
    assert "aucun morceau de ce nom" in reading.rejected[0]


def test_lines_can_come_in_any_order_and_any_number_of_pastes():
    """Pas de numéro, pas de paquet : chaque ligne se suffit à elle-même."""
    first = read_answer("Radiohead | Creep | Rock | Grunge | Sombre | 0.9 | x", LIBRARY, TAXONOMY)
    second = read_answer("Nirvana | Something In The Way | Rock | Acoustic | Calme | 1 | y",
                         LIBRARY, TAXONOMY)

    assert first.verdicts[0].title == "Creep"
    assert second.verdicts[0].title == "Something In The Way"


def test_a_guest_added_to_the_artist_does_not_lose_the_line():
    reading = read_answer("Daft Punk, Pharrell Williams | Get Lucky | Electronic | Disco | Festif",
                          LIBRARY, TAXONOMY)
    assert reading.verdicts[0].artist == "Daft Punk"


def test_the_library_spelling_is_kept_not_the_answer_one():
    """C'est sous l'orthographe de la bibliothèque que le verdict sera retrouvé."""
    reading = read_answer("NIRVANA | something in the way (Official Video) | Rock | Acoustic | Calme",
                          LIBRARY, TAXONOMY)
    assert (reading.verdicts[0].artist, reading.verdicts[0].title) == (
        "Nirvana", "Something In The Way")


def test_a_mood_outside_the_list_is_refused():
    """Une ambiance inventée ouvrirait une neuvième playlist."""
    reading = read_answer("Radiohead | Creep | Rock | Grunge | Nostalgique | 0.9 | x",
                          LIBRARY, TAXONOMY)
    assert reading.verdicts == [] and "Nostalgique" in reading.rejected[0]


def test_a_genre_outside_the_list_is_refused():
    reading = read_answer("Radiohead | Creep | Grunge | Grunge | Sombre", LIBRARY, TAXONOMY)
    assert reading.verdicts == [] and "genre" in reading.rejected[0]


def test_case_and_accents_do_not_cost_a_verdict():
    reading = read_answer("Radiohead | Creep | rock | Grunge | melancolique", LIBRARY, TAXONOMY)
    assert (reading.verdicts[0].genre, reading.verdicts[0].mood) == ("Rock", "Mélancolique")


def test_confidence_and_note_are_optional():
    verdict = read_answer("Radiohead | Creep | Rock | Grunge | Sombre", LIBRARY, TAXONOMY).verdicts[0]
    assert (verdict.confidence, verdict.note) == (0.0, "")


def test_a_comma_decimal_is_understood():
    verdict = read_answer("Radiohead | Creep | Rock | Grunge | Sombre | 0,8", LIBRARY, TAXONOMY)
    assert verdict.verdicts[0].confidence == 0.8


def test_a_markdown_table_is_read_too():
    text = """| artiste | titre | genre | style | ambiance | confiance | note |
|---|---|---|---|---|---|---|
| Radiohead | Creep | Rock | Grunge | Sombre | 0.9 | Ballade. |"""
    assert read_answer(text, LIBRARY, TAXONOMY).verdicts[0].note == "Ballade."


def test_lines_copied_from_the_verdicts_file_are_read_too():
    """Tabulations : la forme même du fichier de résultats."""
    line = "Radiohead\tCreep\tRock\tGrunge\tSombre\t0.90\tclaude\tBallade."
    assert len(read_answer(line, LIBRARY, TAXONOMY).verdicts) == 1


def test_several_blocks_from_continue_are_all_read():
    text = ("```\nNirvana | Something In The Way | Rock | Acoustic | Calme\n```\n\ncontinue\n\n"
            "```\nRadiohead | Creep | Rock | Grunge | Sombre\n```")
    assert len(read_answer(text, LIBRARY, TAXONOMY).verdicts) == 2


def test_a_song_given_twice_counts_once():
    text = ("Radiohead | Creep | Rock | Grunge | Sombre\n"
            "Radiohead | Creep | Rock | Folk | Calme")
    reading = read_answer(text, LIBRARY, TAXONOMY)
    assert [v.style for v in reading.verdicts] == ["Grunge"]


def test_text_without_any_line_explains_how_to_copy():
    with pytest.raises(ChatError, match="Copier"):
        read_answer("Bien sûr, voici mon analyse de ces morceaux…", LIBRARY, TAXONOMY)


def test_an_empty_paste_is_refused():
    with pytest.raises(ChatError):
        read_answer("   ", LIBRARY, TAXONOMY)


# -------------------------------------------------------- de bout en bout


def test_an_import_writes_the_file_and_sorts_the_library(library, config):
    result = import_answer(GOOD, library, config, TAXONOMY)

    assert (result.accepted, result.judged, result.total, result.remaining) == (3, 3, 3, 0)
    assert len(verdicts.load(config.claude.verdicts_file)) == 3
    assert all(c.judged for c in library.classifications())
    assert "3 morceaux jugés sur 3" in result.line()


def test_each_file_shows_how_much_of_it_is_judged(library, config):
    config.claude.export_size = 2
    files = export_library(library, config, TAXONOMY)
    first = files[0].path.read_text(encoding="utf-8").split("TITRES À JUGER")[1]
    titles = [line for line in first.splitlines()[1:] if line.strip()]
    artist, title = titles[0].split(" | ")[:2]

    import_answer(f"{artist} | {title} | Rock | Grunge | Sombre", library, config, TAXONOMY)
    progress = {f.name: (f.judged, f.tracks) for f in export_progress(config)}

    assert progress == {"titres-1-sur-2.txt": (1, 2), "titres-2-sur-2.txt": (0, 1)}


def test_a_hand_corrected_line_survives_an_import(library, config):
    mine = verdicts.manual(Verdict("Radiohead", "Creep", "Rock", "Grunge", "Sombre", 1.0))
    verdicts.save(VerdictBook([mine]), config.claude.verdicts_file)

    import_answer(GOOD, library, config, TAXONOMY)
    kept = verdicts.load(config.claude.verdicts_file).get(mine.key)
    assert (kept.mood, kept.source) == ("Sombre", "manuel")


def test_no_export_means_no_progress_to_show(config):
    assert export_progress(config) == []


def test_a_soundtrack_is_judged_by_its_music_not_its_use():
    """« BO & Scène » n'est plus un genre proposé : un thème de film se range
    selon sa musique, et la ligne qui l'emploie est refusée."""
    reading = read_answer("Radiohead | Creep | BO & Scène | Score | Sombre", LIBRARY, TAXONOMY)
    assert reading.verdicts == [] and "genre" in reading.rejected[0]
    assert "BO & Scène" not in build_file([Subject("A", "B")], 1, 1, TAXONOMY)
