from fakes import FakePlaylistClient

from ytmgc.models import Op, PlaylistPlan, RemotePlaylist
from ytmgc.sync import apply, diff, extract_key, managed_by_key

MARKER = "[ytmgc]"


def managed(playlist_id, title, key, video_ids=()):
    return RemotePlaylist(
        playlist_id=playlist_id,
        title=title,
        description=f"{MARKER} key={key}\n{title}",
        video_ids=tuple(video_ids),
        set_video_ids={v: f"set-{v}" for v in video_ids},
    )


def plan(key, name, video_ids):
    return PlaylistPlan(key, name, f"{MARKER} key={key}\n{name}", tuple(video_ids))


def test_extract_key_ignores_playlists_without_the_marker():
    assert extract_key(f"{MARKER} key=rock/grunge", MARKER) == "rock/grunge"
    assert extract_key("ma playlist perso", MARKER) is None
    assert extract_key(f"{MARKER} sans clé", MARKER) is None


def test_user_playlists_are_invisible_to_the_tool(config):
    """Garde-fou principal : rien sans marqueur n'est jamais touché."""
    remote = [managed("PL1", "Rock — Grunge", "rock/grunge"), RemotePlaylist("PL9", "Perso", "")]
    index = managed_by_key(remote, config.sync.marker)
    assert set(index) == {"rock/grunge"}


def test_missing_playlist_is_created(config):
    actions = diff([plan("rock/grunge", "Rock — Grunge", ["a"])], {}, config)
    assert [a.op for a in actions] == [Op.CREATE]
    assert actions[0].video_ids == ("a",)


def test_existing_playlist_is_reconciled_in_both_directions(config):
    remote = {"rock/grunge": managed("PL1", "Rock — Grunge", "rock/grunge", ["a", "z"])}
    actions = diff([plan("rock/grunge", "Rock — Grunge", ["a", "b"])], remote, config)
    ops = {a.op: a.video_ids for a in actions}
    assert ops[Op.ADD] == ("b",)
    assert ops[Op.REMOVE] == ("z",)


def test_prune_disabled_keeps_extra_tracks(config):
    config.sync.prune = False
    remote = {"rock/grunge": managed("PL1", "Rock — Grunge", "rock/grunge", ["a", "z"])}
    actions = diff([plan("rock/grunge", "Rock — Grunge", ["a"])], remote, config)
    assert actions == []


def test_renamed_playlist_is_recovered_by_its_key(config):
    """L'utilisateur a renommé la playlist : la clé la relie quand même au plan."""
    remote = {"rock/grunge": managed("PL1", "Mon vieux nom", "rock/grunge", ["a"])}
    actions = diff([plan("rock/grunge", "Rock — Grunge", ["a"])], remote, config)
    assert [a.op for a in actions] == [Op.RENAME]
    assert actions[0].new_name == "Rock — Grunge"


def test_obsolete_managed_playlist_is_emptied_not_deleted(config):
    """L'API ne restaure pas une playlist supprimée : on se contente de la vider."""
    remote = {"jazz/bebop": managed("PL2", "Jazz — Bebop", "jazz/bebop", ["x"])}
    actions = diff([], remote, config)
    assert [a.op for a in actions] == [Op.REMOVE]
    assert actions[0].video_ids == ("x",)


def test_dry_run_writes_nothing(config):
    client = FakePlaylistClient()
    plans = [plan("rock/grunge", "Rock — Grunge", ["a"])]
    log = apply(diff(plans, {}, config), plans, client, config, dry_run=True)
    assert client.calls == []
    assert log and log[0].startswith("[à blanc]")


def test_apply_creates_and_records_the_playlist(config):
    client = FakePlaylistClient()
    plans = [plan("rock/grunge", "Rock — Grunge", ["a", "b"])]
    created = {}
    apply(
        diff(plans, {}, config), plans, client, config,
        dry_run=False, on_created=lambda key, pid, name: created.update({key: pid}),
    )
    assert created == {"rock/grunge": "PL1"}
    assert client.get_playlist("PL1").video_ids == ("a", "b")


def test_apply_batches_large_creations(config):
    config.sync.batch_size = 2
    client = FakePlaylistClient()
    plans = [plan("rock/grunge", "Rock — Grunge", list("abcde"))]
    apply(diff(plans, {}, config), plans, client, config, dry_run=False)
    assert client.get_playlist("PL1").video_ids == ("a", "b", "c", "d", "e")
    # Le premier lot part avec la création, les suivants en ajouts successifs.
    assert client.calls == ["create:Rock — Grunge", "add:PL1:2", "add:PL1:1"]


def test_apply_removes_using_set_video_ids(config):
    """La suppression exige le setVideoId, propre à l'appartenance à la playlist."""
    client = FakePlaylistClient([managed("PL1", "Rock — Grunge", "rock/grunge", ["a", "z"])])
    remote = managed_by_key(client.list_playlists(), config.sync.marker)
    plans = [plan("rock/grunge", "Rock — Grunge", ["a"])]
    apply(diff(plans, remote, config), plans, client, config, dry_run=False)
    assert client.get_playlist("PL1").video_ids == ("a",)
