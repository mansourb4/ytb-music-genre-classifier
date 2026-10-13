import tomllib

from ytmgc import master
from ytmgc.master import Photo, Question


def _photo(playlists, library=None, favorites=()):
    """playlists : {pid: (nom, [titres])} ; la bibliothèque, par défaut, leur union."""
    if library is None:
        library = {v for _, ids in playlists.values() for v in ids}
    return Photo(set(library), set(favorites), {k: (n, list(i)) for k, (n, i) in playlists.items()})


BEFORE = {
    "P1": ("Jazz · Modal", ["a", "b", "c"]),
    "P2": ("Soul · Ballades", ["d", "e"]),
}


def test_photo_from_music_keeps_family_folders_and_known_titles():
    raw = {
        "tracks": ["T1", "T2", "CD"],
        "favorites": ["T2"],
        "playlists": [
            {"pid": "P1", "name": "Modal", "folder": "Jazz", "tracks": ["T1", "CD"]},
            {"pid": "P9", "name": "Mes trucs", "folder": "Perso", "tracks": ["T2"]},
        ],
    }
    photo = master.photo_from_music(raw, {"a": "T1", "b": "T2"}, {"Jazz"})
    assert photo.library == {"a", "b"}
    assert photo.favorites == {"b"}
    assert photo.playlists == {"P1": ("Jazz · Modal", ["a"])}


def test_photo_round_trip():
    photo = _photo(BEFORE, favorites={"a"})
    assert Photo.from_json(photo.to_json()) == photo


def test_nothing_changed():
    assert not master.compare(_photo(BEFORE), _photo(BEFORE))


def test_a_move_between_playlists_is_taken_as_is():
    after = _photo({"P1": ("Jazz · Modal", ["a", "c"]), "P2": ("Soul · Ballades", ["d", "e", "b"])})
    changes = master.compare(_photo(BEFORE), after)
    assert changes.moves == {"b": "Soul · Ballades"}
    assert not changes.questions


def test_removed_from_every_playlist_is_asked():
    after = _photo({"P1": ("Jazz · Modal", ["a", "c"]), "P2": ("Soul · Ballades", ["d", "e"])},
                   library={"a", "b", "c", "d", "e"})
    changes = master.compare(_photo(BEFORE), after)
    assert changes.questions == [Question("retiré", "b", ["Jazz · Modal"], [])]
    assert not changes.moves


def test_copied_into_a_second_playlist_is_asked():
    after = _photo({"P1": ("Jazz · Modal", ["a", "b", "c"]),
                    "P2": ("Soul · Ballades", ["d", "e", "b"])})
    changes = master.compare(_photo(BEFORE), after)
    assert changes.questions == [
        Question("ajouté", "b", ["Jazz · Modal"], ["Jazz · Modal", "Soul · Ballades"])]


def test_a_deleted_title_is_not_a_move():
    after = _photo({"P1": ("Jazz · Modal", ["a", "c"]), "P2": ("Soul · Ballades", ["d", "e"])})
    changes = master.compare(_photo(BEFORE), after)
    assert changes.deleted == ["b"]
    assert not changes.moves and not changes.questions


def test_favorites_both_ways():
    changes = master.compare(_photo(BEFORE, favorites={"a", "b"}),
                             _photo(BEFORE, favorites={"b", "c"}))
    assert changes.favorites_added == ["c"]
    assert changes.favorites_removed == ["a"]


def test_a_renamed_playlist_is_not_a_move():
    after = _photo({"P1": ("Jazz · Modal & spirituel", ["a", "b", "c"]),
                    "P2": ("Soul · Ballades", ["d", "e"])})
    changes = master.compare(_photo(BEFORE), after)
    assert changes.renamed == [("Jazz · Modal", "Jazz · Modal & spirituel")]
    assert not changes.moves and not changes.questions


def test_a_new_playlist_takes_its_titles():
    after = _photo({"P1": ("Jazz · Modal", ["a"]), "P2": ("Soul · Ballades", ["d", "e"]),
                    "P3": ("Jazz · Nocturne", ["b", "c"])})
    changes = master.compare(_photo(BEFORE), after)
    assert changes.created == ["Jazz · Nocturne"]
    assert changes.moves == {"b": "Jazz · Nocturne", "c": "Jazz · Nocturne"}


def test_a_deleted_playlist_is_one_question_not_one_per_title():
    after = _photo({"P1": ("Jazz · Modal", ["a", "b", "c"])}, library={"a", "b", "c", "d", "e"})
    changes = master.compare(_photo(BEFORE), after)
    assert changes.questions == [Question("playlist supprimée", None, ["Soul · Ballades"])]
    assert not changes.moves


def test_a_title_dropped_into_a_playlist_from_nowhere():
    after = _photo({"P1": ("Jazz · Modal", ["a", "b", "c", "x"]),
                    "P2": ("Soul · Ballades", ["d", "e"])})
    before = _photo(BEFORE, library={"a", "b", "c", "d", "e", "x"})
    assert master.compare(before, after).moves == {"x": "Jazz · Modal"}


PLAN = """# Rap
[[playlist]]
nom = "Rap · FR posé"
description = "Jazzy."
manuelle = true

# Rock
[[playlist]]
nom = "Rock · Yacht rock"
description = "Doux."
genres = ["Rock"]
"""


def test_plan_edits(tmp_path):
    path = tmp_path / "playlists.toml"
    path.write_text(PLAN, encoding="utf-8")
    master.add_playlist(path, "Jazz · Nocturne", "Créée dans Apple Music.")
    assert master.rename_playlist(path, "Rap · FR posé", 'Rap · FR "posé"')
    assert not master.rename_playlist(path, "Absente", "X")
    assert master.remove_playlist(path, "Rock · Yacht rock")

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    names = [p["nom"] for p in data["playlist"]]
    assert names == ['Rap · FR "posé"', "Jazz · Nocturne"]
    assert data["playlist"][1]["manuelle"] is True
    assert "# Rock" in path.read_text(encoding="utf-8")


def test_decisions_follow_changes_and_answers():
    before = _photo(BEFORE, library={"a", "b", "c", "d", "e"}, favorites={"a"})
    after = _photo({"P1": ("Jazz · Modal", ["a"]), "P3": ("Jazz · Nocturne", ["c"])},
                   library={"a", "b", "c", "d"}, favorites={"d"})
    changes = master.compare(before, after)
    # b retiré sans être rangé ailleurs ; Soul · Ballades supprimée (d y était) ;
    # e supprimé de la bibliothèque ; c déplacé dans une nouvelle playlist.
    kinds = {q.kind: i for i, q in enumerate(changes.questions)}
    answers = {kinds["retiré"]: "Soul · Ballades", kinds["playlist supprimée"]: "supprimer"}
    decisions = master.decide(changes, before, after, answers)
    assert decisions.placements == {
        "b": "Soul · Ballades", "c": "Jazz · Nocturne", "d": master.NOWHERE, "e": master.NOWHERE}
    assert decisions.created == ["Jazz · Nocturne"]
    assert decisions.removed == ["Soul · Ballades"]
    assert decisions.deleted == ["e"]
    assert decisions.like == ["d"]
    assert decisions.unlike == ["a", "e"]


def test_unanswered_questions_change_nothing():
    after = _photo({"P1": ("Jazz · Modal", ["a", "c"]), "P2": ("Soul · Ballades", ["d", "e"])},
                   library={"a", "b", "c", "d", "e"})
    before = _photo(BEFORE)
    decisions = master.decide(master.compare(before, after), before, after, {})
    assert decisions.placements == {}


def test_recreating_a_deleted_playlist_keeps_its_titles_placed():
    after = _photo({"P1": ("Jazz · Modal", ["a", "b", "c"])}, library={"a", "b", "c", "d", "e"})
    before = _photo(BEFORE)
    changes = master.compare(before, after)
    decisions = master.decide(changes, before, after, {0: "recréer"})
    assert decisions.placements == {} and decisions.removed == []
