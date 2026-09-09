from fakes import FakePlaylistClient

from ytmgc.models import RemotePlaylist
from ytmgc.sync import purge

MARKER = "[ytmgc]"


def managed(playlist_id, title, key, video_ids=()):
    return RemotePlaylist(playlist_id, title, f"{MARKER} key={key}\n{title}", tuple(video_ids))


def test_dry_run_deletes_nothing(config):
    client = FakePlaylistClient([managed("PL1", "Rock — Grunge", "rock/grunge", ["a"])])
    log = purge(client.list_playlists(), client, config, dry_run=True)
    assert log[0].startswith("[à blanc]")
    assert len(client.list_playlists()) == 1


def test_purge_removes_every_generated_playlist(config):
    client = FakePlaylistClient([
        managed("PL1", "Rock — Grunge", "rock/grunge", ["a"]),
        managed("PL2", "Electronic — Techno", "electronic/techno", ["b"]),
    ])
    forgotten = []
    purge(client.list_playlists(), client, config, dry_run=False, on_deleted=forgotten.append)
    assert client.list_playlists() == []
    # La suppression ne dépend que du marqueur : elle rend l'identifiant, pas
    # une clé de genre/style dont elle peut fort bien ne rien savoir.
    assert sorted(forgotten) == ["PL1", "PL2"]


def test_purge_never_touches_user_playlists(config):
    """L'annulation reste bornée par le marqueur, comme le reste de l'outil."""
    perso = RemotePlaylist("PLperso", "Rock — Grunge", "ma sélection à moi", ("a",))
    client = FakePlaylistClient([perso, managed("PL1", "Rock — Grunge", "rock/grunge", ["a"])])
    purge(client.list_playlists(), client, config, dry_run=False)
    assert client.list_playlists() == [perso]


def test_purge_on_a_clean_account_is_a_no_op(config):
    client = FakePlaylistClient()
    assert purge(client.list_playlists(), client, config, dry_run=False) == []
    # purge reçoit l'état distant : elle n'interroge pas l'API d'elle-même.
    assert client.calls == ["list"]



def test_a_managed_playlist_without_any_key_is_still_removable(config):
    """La description n'a plus de clé : seul le marqueur autorise la suppression."""
    client = FakePlaylistClient([
        RemotePlaylist("PL1", "Rock — Grunge", "Rock — Grunge\n\nGENRE — Rock\n\n✱", ("a",)),
    ])
    purge(client.list_playlists(), client, config, dry_run=False)
    assert client.list_playlists() == []


def test_playlists_of_the_former_format_are_still_recognised(config):
    """Celles créées avant le changement portent l'ancien marqueur."""
    client = FakePlaylistClient([
        RemotePlaylist("PL1", "Rock — Grunge", "[ytmgc] key=rock/grunge\nRock — Grunge", ("a",)),
    ])
    purge(client.list_playlists(), client, config, dry_run=False)
    assert client.list_playlists() == []
