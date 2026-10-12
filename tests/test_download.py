import subprocess
import unicodedata
from pathlib import Path

from ytmgc import download
from ytmgc.models import PlaylistPlan, Track


def _track(video_id: str, title: str = "Titre", artist: str = "Artiste") -> Track:
    return Track(video_id, title, (artist,), "Album")


def _plan(name: str, *video_ids: str) -> PlaylistPlan:
    return PlaylistPlan(key="plan/" + name, name=name, description="", video_ids=video_ids)


def test_folder_splits_family_and_name():
    assert download.folder_for("Électro · Deep techno") == Path("Électro", "Deep techno")
    assert download.folder_for("Divers") == Path("Divers")


def test_file_name_is_safe_and_carries_the_id():
    track = _track("abcdefghijk", title="Live: 1/2", artist="AC/DC")
    assert download.file_name(track) == "AC-DC - Live- 1-2 [abcdefghijk].m4a"


def test_targets_keep_the_first_playlist_of_a_track():
    tracks = {"aaaaaaaaaaa": _track("aaaaaaaaaaa"), "bbbbbbbbbbb": _track("bbbbbbbbbbb")}
    wanted = download.targets(
        [_plan("Soul · Ballades", "aaaaaaaaaaa"), _plan("Jazz · Modal", "aaaaaaaaaaa", "bbbbbbbbbbb")],
        tracks,
    )
    assert wanted["aaaaaaaaaaa"].parent == Path("Soul", "Ballades")
    assert wanted["bbbbbbbbbbb"].parent == Path("Jazz", "Modal")


def test_organise_moves_downloads_and_sets_aside(tmp_path):
    keep = Path("Soul", "Ballades", "A - T [aaaaaaaaaaa].m4a")
    moved = Path("Soul", "Ballades", "A - T [bbbbbbbbbbb].m4a")
    gone = Path("Jazz", "Modal", "A - T [ccccccccccc].m4a")
    for path in (keep, moved, gone):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_bytes(b"x")
    wanted = {
        "aaaaaaaaaaa": keep,
        "bbbbbbbbbbb": Path("Jazz", "Modal", moved.name),
        "ddddddddddd": Path("Jazz", "Modal", "A - T [ddddddddddd].m4a"),
    }

    local = download.organise(wanted, download.existing(tmp_path))
    assert local.up_to_date == 1
    assert local.downloads == (("ddddddddddd", wanted["ddddddddddd"]),)
    download.apply_moves(tmp_path, local.moves)

    assert (tmp_path / "Jazz" / "Modal" / moved.name).is_file()
    assert (tmp_path / download.OUT_OF_PLAYLISTS / gone.name).is_file()
    assert (tmp_path / keep).is_file()
    # Plus rien à faire au passage suivant, hors téléchargement.
    again = download.organise(wanted, download.existing(tmp_path))
    assert again.moves == ()


def test_empty_folders_are_removed(tmp_path):
    source = Path("Jazz", "Modal", "A - T [aaaaaaaaaaa].m4a")
    (tmp_path / source).parent.mkdir(parents=True)
    (tmp_path / source).write_bytes(b"x")
    (tmp_path / "Jazz" / "Modal" / ".DS_Store").write_bytes(b"")
    download.apply_moves(tmp_path, [download.Move("aaaaaaaaaaa", source, Path("Soul", source.name))])
    assert not (tmp_path / "Jazz").exists()


def test_decomposed_accents_are_not_a_move(tmp_path):
    relative = Path("Électro", "Deep techno", "A - T [aaaaaaaaaaa].m4a")
    on_disk = tmp_path / unicodedata.normalize("NFD", str(relative))
    on_disk.parent.mkdir(parents=True)
    on_disk.write_bytes(b"x")
    present = download.existing(tmp_path)
    assert download.organise({"aaaaaaaaaaa": relative}, present).up_to_date == 1


def test_metadata_comes_from_the_library():
    track = Track("abcdefghijk", "Rain", ("Moderat",), "II: 100%")
    cmd = download.command(Path("yt-dlp"), track, Path("/r/x [abcdefghijk].m4a"))
    assert "%(id)s|Moderat:^[^|]*\\|(?P<meta_artist>.+)$" in cmd
    assert "%(id)s|II\\: 100%%:^[^|]*\\|(?P<meta_album>.+)$" in cmd
    assert cmd[-1] == "https://music.youtube.com/watch?v=abcdefghijk"


def _fake_run(fail: set[str]):
    def run(cmd, **_):
        video_id = cmd[-1].rsplit("=", 1)[1]
        if video_id in fail:
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: Video unavailable")
        output = cmd[cmd.index("--output") + 1].replace("%(ext)s", "m4a").replace("%%", "%")
        Path(output).write_bytes(b"audio")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_download_reports_failures(tmp_path):
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    tracks = {v: _track(v) for v in ids}
    todo = [(v, Path("Soul", download.file_name(tracks[v]))) for v in ids]
    report = download.download(tmp_path, todo, tracks, Path("yt-dlp"),
                               run=_fake_run({"bbbbbbbbbbb"}), sleep=lambda _: None)
    assert report.downloaded == 1
    assert report.failed == [("Artiste – Titre", "ERROR: Video unavailable")]
    assert not report.stopped
    assert set(download.existing(tmp_path)) == {"aaaaaaaaaaa"}


def test_download_stops_when_youtube_blocks(tmp_path):
    ids = [f"{i:011d}" for i in range(10)]
    tracks = {v: _track(v) for v in ids}
    todo = [(v, Path("Soul", download.file_name(tracks[v]))) for v in ids]
    report = download.download(tmp_path, todo, tracks, Path("yt-dlp"),
                               run=_fake_run(set(ids)), sleep=lambda _: None)
    assert report.stopped
    assert len(report.failed) == download.MAX_CONSECUTIVE_FAILURES
