import base64
import subprocess
import unicodedata
from pathlib import Path

import pytest

from ytmgc import download
from ytmgc.models import PlaylistPlan, Track

pytest.importorskip("mutagen")

#: Un dixième de seconde de silence en AAC : un vrai m4a, pour mutagen.
SILENCE = base64.b64decode(
    "AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAAhmcmVlAAAAH21kYXTcAExhdmM2MS4zLjEw"
    "MAACMEAOARggBwAAAwJtb292AAAAbG12aGQAAAAAAAAAAAAAAAAAAAPoAAAAZAABAAABAAAAAAAA"
    "AAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAACAAACLXRyYWsAAABcdGtoZAAAAAMAAAAAAAAAAAAAAAEAAAAAAAAAZAAAAAAA"
    "AAAAAAAAAQEAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAA"
    "ACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAAGQAAAQAAAEAAAAAAaVtZGlhAAAAIG1kaGQAAAAAAAAA"
    "AAAAAAAAAB9AAAAHIFXEAAAAAAAtaGRscgAAAAAAAAAAc291bgAAAAAAAAAAAAAAAFNvdW5kSGFu"
    "ZGxlcgAAAAFQbWluZgAAABBzbWhkAAAAAAAAAAAAAAAkZGluZgAAABxkcmVmAAAAAAAAAAEAAAAM"
    "dXJsIAAAAAEAAAEUc3RibAAAAGpzdHNkAAAAAAAAAAEAAABabXA0YQAAAAAAAAABAAAAAAAAAAAA"
    "AQAQAAAAAB9AAAAAAAA2ZXNkcwAAAAADgICAJQABAASAgIAXQBUAAAAAAB9AAAADJwWAgIAFFYhW"
    "5QAGgICAAQIAAAAgc3R0cwAAAAAAAAACAAAAAQAABAAAAAABAAADIAAAABxzdHNjAAAAAAAAAAEA"
    "AAABAAAAAgAAAAEAAAAcc3RzegAAAAAAAAAAAAAAAgAAABMAAAAEAAAAFHN0Y28AAAAAAAAAAQAA"
    "ACwAAAAac2dwZAEAAAByb2xsAAAAAgAAAAH//wAAABxzYmdwAAAAAHJvbGwAAAABAAAAAgAAAAEA"
    "AABhdWR0YQAAAFltZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAACxp"
    "bHN0AAAAJKl0b28AAAAcZGF0YQAAAAEAAAAATGF2ZjYxLjEuMTAw"
)


def _track(video_id: str, title: str = "Titre", artist: str = "Artiste") -> Track:
    return Track(video_id, title, (artist,), "Album")


def _plan(name: str, *video_ids: str) -> PlaylistPlan:
    return PlaylistPlan(key="plan/" + name, name=name, description="", video_ids=video_ids)


def _tags(genre: str = "Soul · Ballades") -> download.Tags:
    return download.Tags("Titre", "Artiste", "Album", genre, "Soul")


def _file(root: Path, relative: Path | str, video_id: str | None = None) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(SILENCE)
    if video_id:
        download.write_tags(path, video_id, None)
    return path


def test_folder_splits_family_and_name():
    assert download.folder_for("Électro · Deep techno") == Path("Électro", "Deep techno")
    assert download.folder_for("Divers") == Path("Divers")


def test_file_names_are_clean_and_unique():
    tracks = {
        "aaaaaaaaaaa": _track("aaaaaaaaaaa", title="Live: 1/2", artist="AC/DC"),
        "bbbbbbbbbbb": _track("bbbbbbbbbbb", title="Live- 1-2", artist="ac-dc"),
    }
    wanted = download.targets([_plan("Rock", "aaaaaaaaaaa", "bbbbbbbbbbb")], tracks)
    assert wanted["aaaaaaaaaaa"].path == Path("Rock", "AC-DC - Live- 1-2.m4a")
    # Même nom au détail de la casse près : le Mac les confondrait.
    assert wanted["bbbbbbbbbbb"].path == Path("Rock", "ac-dc - Live- 1-2 (2).m4a")


def test_targets_carry_genre_and_style():
    tracks = {"aaaaaaaaaaa": _track("aaaaaaaaaaa"), "bbbbbbbbbbb": _track("bbbbbbbbbbb")}
    wanted = download.targets(
        [_plan("Soul · Ballades", "aaaaaaaaaaa"), _plan("Jazz · Modal", "aaaaaaaaaaa", "bbbbbbbbbbb")],
        tracks,
        {"bbbbbbbbbbb": ("Modal", "Hard Bop", "Free Jazz")},
    )
    assert wanted["aaaaaaaaaaa"].path.parent == Path("Soul", "Ballades")
    assert wanted["aaaaaaaaaaa"].tags.genre == "Soul · Ballades"
    assert wanted["aaaaaaaaaaa"].tags.grouping is None
    assert wanted["bbbbbbbbbbb"].tags.genre == "Jazz · Modal"
    assert wanted["bbbbbbbbbbb"].tags.grouping == "Modal, Hard Bop"


def test_tags_round_trip(tmp_path):
    path = _file(tmp_path, "x.m4a")
    assert download.read_id(path) is None
    assert download.needs_tags(path, "aaaaaaaaaaa", _tags())
    download.write_tags(path, "aaaaaaaaaaa", _tags())
    assert download.read_id(path) == "aaaaaaaaaaa"
    assert not download.needs_tags(path, "aaaaaaaaaaa", _tags())
    assert download.needs_tags(path, "aaaaaaaaaaa", _tags("Jazz · Modal"))


def test_files_are_recognised_by_property_then_old_name(tmp_path):
    _file(tmp_path, "Soul/Ballades/Renommé à la main.m4a", "aaaaaaaaaaa")
    _file(tmp_path, "Soul/Ballades/A - T [bbbbbbbbbbb].m4a")
    _file(tmp_path, "Soul/Ballades/Fichier perso.m4a")
    assert download.existing(tmp_path, download.read_id) == {
        "aaaaaaaaaaa": Path("Soul/Ballades/Renommé à la main.m4a"),
        "bbbbbbbbbbb": Path("Soul/Ballades/A - T [bbbbbbbbbbb].m4a"),
    }


def test_organise_moves_renames_and_sets_aside(tmp_path):
    _file(tmp_path, "Soul/Ballades/A - T.m4a", "aaaaaaaaaaa")
    _file(tmp_path, "Soul/Ballades/A - T [bbbbbbbbbbb].m4a")
    _file(tmp_path, "Jazz/Modal/C - T.m4a", "ccccccccccc")
    wanted = {
        "aaaaaaaaaaa": download.Wanted(Path("Soul/Ballades/A - T.m4a"), _tags()),
        "bbbbbbbbbbb": download.Wanted(Path("Jazz/Modal/B - T.m4a"), _tags("Jazz · Modal")),
        "ddddddddddd": download.Wanted(Path("Jazz/Modal/D - T.m4a"), _tags("Jazz · Modal")),
    }
    present = download.existing(tmp_path, download.read_id)
    local = download.organise(wanted, present)
    assert local.up_to_date == 1
    assert local.downloads == (("ddddddddddd", Path("Jazz/Modal/D - T.m4a")),)

    assert download.retag(tmp_path, wanted, present) == 2
    download.apply_moves(tmp_path, local.moves)

    moved = tmp_path / "Jazz/Modal/B - T.m4a"
    assert download.read_id(moved) == "bbbbbbbbbbb"
    assert not download.needs_tags(moved, "bbbbbbbbbbb", _tags("Jazz · Modal"))
    assert download.read_id(tmp_path / download.OUT_OF_PLAYLISTS / "C - T.m4a") == "ccccccccccc"
    assert not (tmp_path / "Soul/Ballades/A - T [bbbbbbbbbbb].m4a").exists()
    assert not (tmp_path / download.STAGING).exists()
    again = download.organise(wanted, download.existing(tmp_path, download.read_id))
    assert again.moves == ()


def test_two_files_can_swap_names(tmp_path):
    _file(tmp_path, "Rock/X.m4a", "aaaaaaaaaaa")
    _file(tmp_path, "Rock/X (2).m4a", "bbbbbbbbbbb")
    download.apply_moves(tmp_path, [
        download.Move("aaaaaaaaaaa", Path("Rock/X.m4a"), Path("Rock/X (2).m4a")),
        download.Move("bbbbbbbbbbb", Path("Rock/X (2).m4a"), Path("Rock/X.m4a")),
    ])
    assert download.read_id(tmp_path / "Rock/X.m4a") == "bbbbbbbbbbb"
    assert download.read_id(tmp_path / "Rock/X (2).m4a") == "aaaaaaaaaaa"


def test_empty_folders_are_removed(tmp_path):
    _file(tmp_path, "Jazz/Modal/A - T.m4a", "aaaaaaaaaaa")
    (tmp_path / "Jazz" / "Modal" / ".DS_Store").write_bytes(b"")
    download.apply_moves(tmp_path, [
        download.Move("aaaaaaaaaaa", Path("Jazz/Modal/A - T.m4a"), Path("Soul/A - T.m4a")),
    ])
    assert not (tmp_path / "Jazz").exists()


def test_decomposed_accents_are_not_a_move(tmp_path):
    relative = Path("Électro", "Deep techno", "A - T.m4a")
    _file(tmp_path, unicodedata.normalize("NFD", str(relative)), "aaaaaaaaaaa")
    present = download.existing(tmp_path, download.read_id)
    wanted = {"aaaaaaaaaaa": download.Wanted(relative, _tags())}
    assert download.organise(wanted, present).up_to_date == 1


def _fake_run(fail: set[str], error: str = "ERROR: Video unavailable", flaky: int = 0):
    calls = {"n": 0}

    def run(cmd, **_):
        video_id = cmd[-1].rsplit("=", 1)[1]
        calls["n"] += 1
        if video_id in fail or calls["n"] <= flaky:
            return subprocess.CompletedProcess(cmd, 1, "", error)
        output = cmd[cmd.index("--output") + 1].replace("%(ext)s", "m4a").replace("%%", "%")
        Path(output).write_bytes(SILENCE)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_download_tags_then_files_and_reports_failures(tmp_path):
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    tracks = {v: _track(v) for v in ids}
    wanted = {v: download.Wanted(Path("Soul", f"{v}.m4a"), _tags()) for v in ids}
    todo = [(v, wanted[v].path) for v in ids]
    report = download.download(tmp_path, todo, tracks, wanted, Path("yt-dlp"),
                               run=_fake_run({"bbbbbbbbbbb"}), sleep=lambda _: None)
    assert report.downloaded == 1
    assert report.failed == [("Artiste – Titre", "ERROR: Video unavailable")]
    assert not report.stopped
    done = tmp_path / "Soul" / "aaaaaaaaaaa.m4a"
    assert not download.needs_tags(done, "aaaaaaaaaaa", _tags())
    assert set(download.existing(tmp_path, download.read_id)) == {"aaaaaaaaaaa"}


def test_download_stops_when_youtube_blocks(tmp_path):
    ids = [f"{i:011d}" for i in range(10)]
    tracks = {v: _track(v) for v in ids}
    wanted = {v: download.Wanted(Path("Soul", f"{v}.m4a"), _tags()) for v in ids}
    report = download.download(tmp_path, [(v, wanted[v].path) for v in ids], tracks, wanted,
                               Path("yt-dlp"), run=_fake_run(set(ids)), sleep=lambda _: None)
    assert report.stopped
    assert len(report.failed) == download.MAX_CONSECUTIVE_FAILURES


def test_a_passing_403_is_retried(tmp_path):
    tracks = {"aaaaaaaaaaa": _track("aaaaaaaaaaa")}
    wanted = {"aaaaaaaaaaa": download.Wanted(Path("Soul", "a.m4a"), _tags())}
    waits = []
    report = download.download(
        tmp_path, [("aaaaaaaaaaa", wanted["aaaaaaaaaaa"].path)], tracks, wanted, Path("yt-dlp"),
        run=_fake_run(set(), "ERROR: unable to download video data: HTTP Error 403: Forbidden",
                      flaky=2),
        sleep=waits.append,
    )
    assert report.downloaded == 1 and not report.failed
    assert waits == list(download.RETRY_DELAYS)


def test_a_lasting_error_is_not_retried(tmp_path):
    tracks = {"aaaaaaaaaaa": _track("aaaaaaaaaaa")}
    wanted = {"aaaaaaaaaaa": download.Wanted(Path("Soul", "a.m4a"), _tags())}
    waits = []
    report = download.download(
        tmp_path, [("aaaaaaaaaaa", wanted["aaaaaaaaaaa"].path)], tracks, wanted, Path("yt-dlp"),
        run=_fake_run({"aaaaaaaaaaa"}), sleep=waits.append,
    )
    assert len(report.failed) == 1 and waits == []
