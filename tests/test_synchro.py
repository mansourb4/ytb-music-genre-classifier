"""L'écriture des décisions d'une synchro : plan, déplacements, base, YouTube."""

import tomllib

from ytmgc import cli, master, placements
from ytmgc.config import Config
from ytmgc.matching.normalize import slugify
from ytmgc.models import Track
from ytmgc.playlist_plan import load_plan
from ytmgc.store import Repository, connect
from ytmgc.taxonomy import load_taxonomy

PLAN = """[[playlist]]
nom = "Jazz · Modal"
description = "Modal."
manuelle = true

[[playlist]]
nom = "Soul · Ballades"
description = "Lent."
manuelle = true
"""


class FakeClient:
    def __init__(self):
        self.calls = []

    def like(self, video_id):
        self.calls.append(("like", video_id))

    def unlike(self, video_id):
        self.calls.append(("unlike", video_id))

    def remove_from_library(self, video_ids):
        self.calls.append(("remove", tuple(video_ids)))
        return len(video_ids)


def test_decisions_are_written_everywhere(tmp_path, monkeypatch):
    config = Config()
    config.taxonomy.playlists_file = str(tmp_path / "playlists.toml")
    config.taxonomy.placements_file = str(tmp_path / "placements.txt")
    (tmp_path / "playlists.toml").write_text(PLAN, encoding="utf-8")
    tracks = {
        "a": Track("a", "So What", ("Miles Davis",)),
        "b": Track("b", "Rain", ("Moderat",)),
        "e": Track("e", "Effacé", ("Quelqu'un",)),
    }
    repository = Repository(connect(":memory:"))
    repository.upsert_tracks(list(tracks.values()))
    repository.remember_playlist("plan/" + slugify("Jazz · Modal"), "PLyt1", "Jazz · Modal")
    book = placements.PlacementBook()
    book.put(tracks["a"], "Jazz · Modal")
    placements.save(book, config.taxonomy.placements_file)
    client = FakeClient()
    monkeypatch.setattr(cli, "_youtube", lambda _config: client)

    decisions = master.Decisions(
        placements={"b": "Jazz · Nocturne", "e": master.NOWHERE},
        created=["Jazz · Nocturne"],
        renamed=[("Jazz · Modal", "Jazz · Modal & spirituel")],
        removed=["Soul · Ballades"],
        deleted=["e"],
        like=["b"],
        unlike=["e"],
    )
    cli._synchro_apply(decisions, tracks, repository, config, slugify, placements, master,
                       load_plan)

    names = [p["nom"] for p in tomllib.loads(
        (tmp_path / "playlists.toml").read_text(encoding="utf-8"))["playlist"]]
    assert names == ["Jazz · Modal & spirituel", "Jazz · Nocturne"]
    assert {p.name for p in load_plan(config.taxonomy.playlists_file, load_taxonomy()).playlists}
    lines = {(p.artist, p.title): p.playlist
             for p in placements.load(config.taxonomy.placements_file)}
    assert lines == {
        ("Miles Davis", "So What"): "Jazz · Modal & spirituel",
        ("Moderat", "Rain"): "Jazz · Nocturne",
        ("Quelqu'un", "Effacé"): master.NOWHERE,
    }
    managed = repository.managed_playlists()
    assert managed == {"plan/" + slugify("Jazz · Modal & spirituel"):
                       ("PLyt1", "Jazz · Modal & spirituel")}
    assert {t.video_id for t in repository.all_tracks()} == {"a", "b"}
    assert client.calls == [("like", "b"), ("unlike", "e"), ("remove", ("e",))]


def test_a_dry_run_lists_the_changes_made_in_music(tmp_path, monkeypatch, capsys):
    from ytmgc import apple_music

    root = tmp_path / "Music"
    root.mkdir()
    state = apple_music.State({"a": "T1", "b": "T2"}, {"Jazz", "Soul"})
    state.photo = master.Photo({"a", "b"}, {"a"}, {
        "P1": ("Jazz · Modal", ["a", "b"]), "P2": ("Soul · Ballades", [])}).to_json()
    apple_music.save_state(root, state)
    raw = {"tracks": ["T1", "T2"], "favorites": [],
           "playlists": [{"pid": "P1", "name": "Modal", "folder": "Jazz", "tracks": ["T1"]},
                         {"pid": "P2", "name": "Ballades", "folder": "Soul", "tracks": ["T2"]}]}
    monkeypatch.setattr(apple_music, "_osascript", lambda *a, **k: raw)
    config = Config()
    config.store.path = str(tmp_path / "db.sqlite")
    repository = Repository(connect(config.store.path))
    repository.upsert_tracks([Track("a", "So What", ("Miles Davis",)),
                              Track("b", "Rain", ("Moderat",))])

    args = cli.build_parser().parse_args(["synchro", "--dossier", str(root), "--essai"])
    assert cli.cmd_synchro(args, config) == 0
    out = capsys.readouterr().out
    assert "Moderat – Rain → Soul · Ballades" in out
    assert "Favoris retirés (1)" in out and "Miles Davis – So What" in out
    assert "Essai : rien n'a été modifié." in out
    # L'essai ne touche pas à la photo.
    assert apple_music.load_state(root).photo == state.photo
