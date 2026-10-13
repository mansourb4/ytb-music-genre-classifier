"""Le script Apple Music, joué sous Node contre une fausse application Music."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ytmgc import apple_music
from ytmgc.download import Tags, Wanted
from ytmgc.models import PlaylistPlan

NODE = shutil.which("node")
FAKE = Path(__file__).with_name("apple_music_fake.js")
ROOT = Path("/Users/x/Music/Playlists")


def _wanted(genre: str, name: str) -> Wanted:
    family, leaf = genre.split(" · ")
    return Wanted(Path(family, leaf, f"{name}.m4a"), Tags(name, "Artiste", None, genre, "Style"))


def _plan(name: str, *ids: str) -> PlaylistPlan:
    return PlaylistPlan(key="plan/" + name, name=name, description="", video_ids=ids)


class Library:
    """Une bibliothèque Apple Music imitée, conservée d'un lancement à l'autre."""

    def __init__(self, tmp_path: Path, *, copies: bool = False, playlists=()):
        self.path = tmp_path / "music.json"
        self.path.write_text(json.dumps(
            {"copies": copies, "tracks": [], "playlists": list(playlists)}))

    @property
    def state(self) -> dict:
        return json.loads(self.path.read_text())

    def popen(self, cmd, **kwargs):
        assert cmd[:3] == ["osascript", "-l", "JavaScript"]
        return subprocess.Popen([NODE, str(FAKE), cmd[3], str(self.path), cmd[4]], **kwargs)

    def run(self, plans, wanted, present, known=None, dry_run=False, families=()):
        state = apple_music.State(dict(known or {}), set(families))
        payload = apple_music.build_input(ROOT, plans, wanted, present, state,
                                          dry_run=dry_run)
        return apple_music.run_script(payload, on_line=lambda line: None, popen=self.popen)

    def playlist(self, family: str, name: str) -> list[str]:
        state = self.state
        folder = next(p for p in state["playlists"] if p["folder"] and p["name"] == family)
        playlist = next(p for p in state["playlists"]
                        if p["name"] == name and p["parent"] == folder["pid"])
        by_pid = {t["pid"]: t["path"] for t in state["tracks"]}
        return [Path(by_pid[pid]).stem for pid in playlist["tracks"]]


pytestmark = pytest.mark.skipif(NODE is None, reason="Node absent")


def test_split_name():
    assert apple_music.split_name("Électro · Deep techno") == ("Électro", "Deep techno")
    assert apple_music.split_name("Divers") == (apple_music.NO_FAMILY, "Divers")


def _setup():
    wanted = {
        "a": _wanted("Électro · Deep techno", "A"),
        "b": _wanted("Électro · Deep techno", "B"),
        "c": _wanted("Jazz · Modal", "C"),
    }
    plans = [_plan("Électro · Deep techno", "a", "b"), _plan("Jazz · Modal", "c")]
    present = {v: item.path for v, item in wanted.items()}
    return wanted, plans, present


def test_first_run_builds_folders_and_playlists(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    result = library.run(plans, wanted, present)

    assert result.added == 3 and not result.missing and not result.copied
    assert sorted(result.created_folders) == ["Jazz", "Électro"]
    assert sorted(result.created_playlists) == ["Jazz/Modal", "Électro/Deep techno"]
    assert library.playlist("Électro", "Deep techno") == ["A", "B"]
    assert library.playlist("Jazz", "Modal") == ["C"]
    assert {t["genre"] for t in library.state["tracks"]} == {"Électro · Deep techno",
                                                             "Jazz · Modal"}
    assert set(result.known) == {"a", "b", "c"}


def test_a_second_run_changes_nothing(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    again = library.run(plans, wanted, present, known)
    assert (again.added, again.relocated, again.retagged, again.track_adds,
            again.track_removals) == (0, 0, 0, 0, 0)
    assert not again.created_folders and not again.created_playlists
    assert len(library.state["tracks"]) == 3


def test_a_title_that_changes_playlist_follows(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known

    # « b » passe en Jazz · Modal : fichier déplacé, genre et playlists changés.
    wanted["b"] = _wanted("Jazz · Modal", "B")
    present["b"] = wanted["b"].path
    plans = [_plan("Électro · Deep techno", "a"), _plan("Jazz · Modal", "c", "b")]
    result = library.run(plans, wanted, present, known)

    assert (result.added, result.relocated, result.retagged) == (0, 1, 1)
    assert (result.track_adds, result.track_removals) == (1, 1)
    assert library.playlist("Électro", "Deep techno") == ["A"]
    assert library.playlist("Jazz", "Modal") == ["C", "B"]
    moved = next(t for t in library.state["tracks"] if t["pid"] == known["b"])
    assert moved["path"] == str(ROOT / "Jazz" / "Modal" / "B.m4a")


def test_own_playlists_are_left_alone(tmp_path):
    mine = {"pid": "MINE", "name": "Deep techno", "folder": False, "parent": None,
            "tracks": []}
    library = Library(tmp_path, playlists=[mine])
    wanted, plans, present = _setup()
    library.run(plans, wanted, present)
    untouched = next(p for p in library.state["playlists"] if p["pid"] == "MINE")
    assert untouched["tracks"] == [] and untouched["parent"] is None


def test_playlists_dropped_from_the_plan_are_reported_not_deleted(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    result = library.run(plans[:1], {k: wanted[k] for k in "ab"},
                         {k: present[k] for k in "ab"}, known, families={"Électro", "Jazz"})
    assert result.stale == ["Jazz/Modal"]
    assert library.playlist("Jazz", "Modal") == ["C"]


def test_copying_into_the_music_folder_stops_everything(tmp_path):
    library = Library(tmp_path, copies=True)
    wanted, plans, present = _setup()
    result = library.run(plans, wanted, present)
    assert result.copied and "Media" in result.copied_to
    assert library.state["tracks"] == [] and library.state["playlists"] == []


def test_a_dry_run_touches_nothing(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    result = library.run(plans, wanted, present, dry_run=True)
    assert result.added == 3 and result.track_adds == 3
    assert len(result.created_playlists) == 2
    assert library.state["tracks"] == [] and library.state["playlists"] == []


def test_state_round_trip(tmp_path):
    apple_music.save_state(tmp_path, apple_music.State({"a": "P1"}, {"Jazz"}))
    assert apple_music.load_state(tmp_path) == apple_music.State({"a": "P1"}, {"Jazz"})
    assert apple_music.load_state(tmp_path / "absent") == apple_music.State()


# ------------------------------------------------------------------ favoris

def _favorites(library, pids, dry_run=False, reorder=False, remove=False):
    return apple_music.run_favorites(pids, dry_run=dry_run, reorder=reorder, remove=remove,
                                     on_line=lambda line: None, popen=library.popen)


def _shown(library):
    """Les favoris tels qu'Apple Music les montre : le plus récent d'abord."""
    favorites = [t for t in library.state["tracks"] if t.get("favorite")]
    return [t["pid"] for t in sorted(favorites, key=lambda t: -t["favoriteAt"])]


def test_liked_titles_become_favorites(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known

    result = _favorites(library, [known["a"], known["c"], "DISPARU"])
    assert (result.favorited, result.already, result.gone) == (2, 0, 1)
    favorites = {t["pid"] for t in library.state["tracks"] if t.get("favorite")}
    assert favorites == {known["a"], known["c"]}

    again = _favorites(library, [known["a"], known["c"]])
    assert (again.favorited, again.already) == (0, 2)


def test_favorites_on_an_older_macos(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    state = library.state
    state["ancienMacOS"] = True
    library.path.write_text(json.dumps(state))
    assert _favorites(library, [known["b"]]).favorited == 1
    assert [t["pid"] for t in library.state["tracks"] if t.get("favorite")] == [known["b"]]


def test_a_dry_run_sets_no_favorite(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    assert _favorites(library, [known["a"]], dry_run=True).favorited == 1
    assert not any(t.get("favorite") for t in library.state["tracks"])


def test_liked_titles_are_matched_by_id_then_by_song():
    from ytmgc.models import Track

    tracks = {
        "a": Track("a", "Rain", ("Moderat",)),
        "b": Track("b", "Aquarela do Brasil", ("João Gilberto",)),
    }
    known = {"a": "P1", "b": "P2"}
    liked = [
        {"video_id": "a", "label": "Moderat – Rain"},
        # Doublon : l'autre version du morceau, téléchargée sous « b ».
        {"video_id": "z", "label": "Joao Gilberto – Aquarela Do Brasil",
         "title": "Aquarela Do Brasil", "artists": ["Joao Gilberto"]},
        {"video_id": "y", "label": "Inconnu – Rien", "title": "Rien", "artists": ["Inconnu"]},
        {"video_id": None, "label": "Grisé"},
        {"video_id": "a", "label": "Moderat – Rain"},
    ]
    pids, absent = apple_music.favorite_ids(liked, tracks, known)
    assert pids == ["P1", "P2"]
    assert absent == ["Inconnu – Rien"]


def test_absent_titles_are_grouped_by_reason():
    from ytmgc.models import Track

    library = {
        "in": Track("in", "Rain", ("Moderat",)),
        "twin": Track("twin", "rain", ("moderat",)),          # doublon de « in »
        "todo": Track("todo", "Pas importé", ("A",)),          # fichier là, pas dans Music
        "fail": Track("fail", "Copain", ("Roméo Elvis",)),     # téléchargement échoué
        "none": Track("none", "Born 2 Die", ("Moodymann",)),   # dans aucune playlist
    }
    liked = [
        {"video_id": "in", "label": "Moderat – Rain"},
        {"video_id": "perso", "label": "B – Perso", "title": "Perso", "artists": ["B"]},
        {"video_id": "perso", "label": "B – Perso", "title": "Perso", "artists": ["B"]},
        {"video_id": None, "label": "Grisé"},
    ]
    groups = apple_music.absent_titles(
        library, liked, wanted={"in", "todo", "fail"}, present={"in", "todo"},
        known={"in": "P1"},
    )
    assert groups == {
        apple_music.NOT_IN_MUSIC_YET: ["A – Pas importé"],
        apple_music.DOWNLOAD_FAILED: ["Roméo Elvis – Copain"],
        apple_music.IN_NO_PLAYLIST: ["Moodymann – Born 2 Die"],
        apple_music.LIKED_NOT_IN_LIBRARY: ["B – Perso"],
        apple_music.LIKED_REMOVED: ["Grisé"],
    }


def test_favorites_show_in_the_order_of_the_likes(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    likes = [known["a"], known["b"], known["c"]]          # « a » liké le plus récemment
    _favorites(library, likes)
    assert _shown(library) == likes
    assert library.state["delays"] == 2                    # une pause entre deux favoris


def test_a_new_like_lands_on_top_without_redoing_the_others(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    _favorites(library, [known["b"], known["c"]])
    result = _favorites(library, [known["a"], known["b"], known["c"]])
    assert (result.favorited, result.already) == (1, 2)
    assert _shown(library) == [known["a"], known["b"], known["c"]]


def test_reordering_fixes_favorites_added_in_the_wrong_order(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    likes = [known["a"], known["b"], known["c"]]
    _favorites(library, likes[::-1])                       # l'ordre inverse
    assert _shown(library) == likes[::-1]
    assert _favorites(library, likes, reorder=True).favorited == 3
    assert _shown(library) == likes


# ------------------------------------------------------------ synchro

def test_reading_music_then_spotting_a_hand_move(tmp_path):
    from ytmgc import master

    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    _favorites(library, [known["c"]])

    def photo():
        raw = apple_music._osascript(master.READ_SCRIPT, {}, on_line=lambda line: None,
                                     popen=library.popen)
        return master.photo_from_music(raw, known, {"Électro", "Jazz"})

    before = photo()
    assert before.library == {"a", "b", "c"} and before.favorites == {"c"}
    assert sorted(name for name, _ in before.playlists.values()) == [
        "Jazz · Modal", "Électro · Deep techno"]

    # À la main dans Music : « b » glissé de Deep techno vers Modal.
    state = library.state
    by_name = {p["name"]: p for p in state["playlists"]}
    by_name["Deep techno"]["tracks"].remove(known["b"])
    by_name["Modal"]["tracks"].append(known["b"])
    library.path.write_text(json.dumps(state))

    changes = master.compare(before, photo())
    assert changes.moves == {"b": "Jazz · Modal"}
    assert not changes.questions and not changes.deleted


def test_remove_then_favorite_again_in_order(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    likes = [known["a"], known["b"], known["c"]]
    _favorites(library, likes[::-1])                       # l'ordre inverse

    assert _favorites(library, likes, remove=True).removed == 3
    assert _shown(library) == []                           # l'iPhone se vide ici
    assert _favorites(library, likes).favorited == 3
    assert _shown(library) == likes


def _by_date_added(library):
    """Les favoris triés par date d'ajout, le plus récent d'abord : la vue de Music."""
    favorites = [t for t in library.state["tracks"] if t.get("favorite")]
    return [t["path"] for t in sorted(favorites, key=lambda t: -t["addedAt"])]


def test_reimport_puts_favorites_in_the_order_of_the_likes(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    likes = [known["c"], known["a"], known["b"]]                # « c » liké le plus récemment
    _favorites(library, likes)

    result = apple_music.run_favorites(likes, reimport=True, on_line=lambda line: None,
                                       popen=library.popen)
    assert set(result.reimported) == set(likes)
    assert not set(result.reimported.values()) & set(likes)     # nouveaux identifiants
    assert len(library.state["tracks"]) == 3                   # pas de doublon
    paths = {v: str(ROOT / wanted[v].path) for v in "abc"}
    assert _by_date_added(library) == [paths["c"], paths["a"], paths["b"]]

    # Les nouveaux identifiants retenus, apple-music remet les titres dans leurs playlists.
    renumbered = {v: result.reimported[pid] for v, pid in known.items()}
    again = library.run(plans, wanted, present, renumbered)
    assert again.added == 0
    assert library.playlist("Électro", "Deep techno") == ["A", "B"]
    assert library.playlist("Jazz", "Modal") == ["C"]


def test_reimport_stops_if_a_file_vanishes(tmp_path):
    library = Library(tmp_path)
    wanted, plans, present = _setup()
    known = library.run(plans, wanted, present).known
    state = library.state
    state["fichiersPerdus"] = True
    library.path.write_text(json.dumps(state))
    with pytest.raises(RuntimeError, match="fichier disparu"):
        apple_music.run_favorites([known["a"]], reimport=True, on_line=lambda line: None,
                                  popen=library.popen)
