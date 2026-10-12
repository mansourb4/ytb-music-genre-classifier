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
    long = "x" * 200
    tracks = {
        "aaaaaaaaaaa": _track("aaaaaaaaaaa", title="Live: 1/2", artist="AC/DC"),
        "bbbbbbbbbbb": _track("bbbbbbbbbbb", title=long + " A"),
        "ccccccccccc": _track("ccccccccccc", title=long.upper() + " B"),
    }
    wanted = download.targets([_plan("Rock", *tracks)], tracks)
    assert wanted["aaaaaaaaaaa"].path == Path("Rock", "AC-DC - Live- 1-2.m4a")
    # Deux morceaux distincts, même nom une fois tronqué, au détail de la
    # casse près : le Mac les confondrait.
    assert wanted["ccccccccccc"].path.stem == "Artiste - " + "X" * 140 + " (2)"


def test_targets_carry_genre_and_style():
    tracks = {"aaaaaaaaaaa": _track("aaaaaaaaaaa", title="A"),
              "bbbbbbbbbbb": _track("bbbbbbbbbbb", title="B")}
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
        sleep=waits.append, pause=0,
    )
    assert report.downloaded == 1 and not report.failed
    assert waits == list(download.RETRY_DELAYS)


def test_a_lasting_error_is_not_retried(tmp_path):
    tracks = {"aaaaaaaaaaa": _track("aaaaaaaaaaa")}
    wanted = {"aaaaaaaaaaa": download.Wanted(Path("Soul", "a.m4a"), _tags())}
    waits = []
    report = download.download(
        tmp_path, [("aaaaaaaaaaa", wanted["aaaaaaaaaaa"].path)], tracks, wanted, Path("yt-dlp"),
        run=_fake_run({"aaaaaaaaaaa"}), sleep=waits.append, pause=0,
    )
    assert len(report.failed) == 1 and waits == []


def test_parallel_downloads_get_everything(tmp_path):
    ids = [f"{i:011d}" for i in range(12)]
    tracks = {v: _track(v) for v in ids}
    wanted = {v: download.Wanted(Path("Soul", f"{v}.m4a"), _tags()) for v in ids}
    seen = []
    report = download.download(
        tmp_path, [(v, wanted[v].path) for v in ids], tracks, wanted, Path("yt-dlp"),
        workers=4, run=_fake_run({ids[3]}), sleep=lambda _: None,
        on_progress=lambda done, total, track, error: seen.append(done),
    )
    assert report.downloaded == 11 and len(report.failed) == 1
    assert seen == list(range(1, 13))
    assert set(download.existing(tmp_path, download.read_id)) == set(ids) - {ids[3]}


def test_a_second_run_downloads_nothing(tmp_path):
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    tracks = {v: _track(v, title=v) for v in ids}
    wanted = download.targets([_plan("Électro · Deep techno", *ids)], tracks)
    first = download.organise(wanted, download.existing(tmp_path, download.read_id))
    download.download(tmp_path, first.downloads, tracks, wanted, Path("yt-dlp"),
                      run=_fake_run(set()), sleep=lambda _: None)
    again = download.organise(wanted, download.existing(tmp_path, download.read_id))
    assert again.downloads == () and again.moves == () and again.up_to_date == 2


def test_the_same_song_twice_is_downloaded_once():
    tracks = {
        "aaaaaaaaaaa": _track("aaaaaaaaaaa", title="Aquarela do Brasil", artist="João Gilberto"),
        "bbbbbbbbbbb": _track("bbbbbbbbbbb", title="Aquarela Do Brasil", artist="Joao Gilberto"),
    }
    duplicates = []
    wanted = download.targets(
        [_plan("Brésil · Bossa nova", "aaaaaaaaaaa"), _plan("Jazz · Modal", "bbbbbbbbbbb")],
        tracks, duplicates=duplicates,
    )
    assert list(wanted) == ["aaaaaaaaaaa"]
    assert duplicates == ["bbbbbbbbbbb"]


def test_files_from_elsewhere_are_adopted_by_name(tmp_path):
    tracks = {
        "aaaaaaaaaaa": _track("aaaaaaaaaaa", title="The Girl From Ipanema", artist="Stan Getz"),
        "bbbbbbbbbbb": _track("bbbbbbbbbbb", title="Insensatez", artist="Toquinho"),
        "ccccccccccc": _track("ccccccccccc", title="Solo", artist="Roméo Elvis"),
        "ddddddddddd": _track("ddddddddddd", title="Solo", artist="Autre"),
    }
    wanted = download.targets([_plan("Brésil · Bossa nova", *tracks)], tracks)
    _file(tmp_path, "Stacher/Stan Getz - The Girl from Ipanema.m4a")
    _file(tmp_path, "Stacher/Insensatez (2).m4a")
    _file(tmp_path, "Stacher/Solo.m4a")  # deux « Solo » : ambigu, laissé tranquille
    unknown = []
    present = download.existing(tmp_path, download.read_id, unknown=unknown)
    adopted = download.adopt(wanted, tracks, present, unknown)
    assert adopted == {
        "aaaaaaaaaaa": Path("Stacher/Stan Getz - The Girl from Ipanema.m4a"),
        "bbbbbbbbbbb": Path("Stacher/Insensatez (2).m4a"),
    }

    present = {**present, **adopted}
    local = download.organise(wanted, present)
    assert {v for v, _ in local.downloads} == {"ccccccccccc", "ddddddddddd"}
    download.retag(tmp_path, wanted, present)
    download.apply_moves(tmp_path, local.moves)
    target = tmp_path / wanted["aaaaaaaaaaa"].path
    assert download.read_id(target) == "aaaaaaaaaaa"
    assert (tmp_path / "Stacher" / "Solo.m4a").is_file()


def test_low_bitrate_streams_come_last():
    yt_dlp = pytest.importorskip("yt_dlp")
    pick = yt_dlp.YoutubeDL({"quiet": True}).build_format_selector(download.AUDIO_FORMAT)

    def chosen(*formats):
        available = [{"format_id": f, "ext": e, "acodec": "x", "vcodec": "none", "abr": a,
                      "url": "u", "protocol": "https"} for f, e, a in formats]
        return [f["format_id"] for f in pick({"formats": available})]

    low, aac, opus, premium = ("139", "m4a", 48), ("140", "m4a", 129), ("251", "webm", 135), \
        ("141", "m4a", 256)
    assert chosen(low, aac, opus) == ["140"]
    assert chosen(low, aac, opus, premium) == ["141"]
    assert chosen(low, opus) == ["251"]  # plutôt que l'AAC à 48 kbps
    assert chosen(low) == ["139"]


def test_bitrate_is_read(tmp_path):
    assert isinstance(download.kbps(_file(tmp_path, "x.m4a")), int)
    (tmp_path / "abîmé.m4a").write_bytes(b"pas du son")
    assert download.kbps(tmp_path / "abîmé.m4a") is None


def test_each_download_gets_its_own_cookie_copy(tmp_path):
    original = tmp_path / "cookies.txt"
    original.write_text("# Netscape HTTP Cookie File\n")
    ids = [f"{i:011d}" for i in range(6)]
    tracks = {v: _track(v, title=v) for v in ids}
    wanted = {v: download.Wanted(Path("Soul", f"{v}.m4a"), _tags()) for v in ids}
    seen = []
    fake = _fake_run(set())

    def run(cmd, **kwargs):
        jar = Path(cmd[cmd.index("--cookies") + 1])
        assert jar.read_text() == original.read_text()
        jar.write_text("réécrit par yt-dlp")
        seen.append(jar)
        return fake(cmd, **kwargs)

    root = tmp_path / "Music"
    report = download.download(root, [(v, wanted[v].path) for v in ids], tracks, wanted,
                               Path("yt-dlp"), cookies_file=original, workers=3, run=run,
                               sleep=lambda _: None)
    assert report.downloaded == 6
    assert len(set(seen)) == 6
    assert not any(jar.exists() for jar in seen)
    assert not any(root in jar.parents for jar in seen)
    assert original.read_text() == "# Netscape HTTP Cookie File\n"
