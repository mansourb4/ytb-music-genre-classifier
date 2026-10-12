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
