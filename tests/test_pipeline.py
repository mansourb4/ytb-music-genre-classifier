"""Test de bout en bout : scan -> classify -> plan -> apply, sans réseau."""

from conftest import make_candidate
from fakes import FakeDiscogs, FakePlaylistClient

from ytmgc.classifier import classify_tracks
from ytmgc.models import RemotePlaylist, Track
from ytmgc.planner import plan_playlists
from ytmgc.sync import apply, diff, managed_by_key
from ytmgc.taxonomy import load_taxonomy

CATALOGUE = {
    "Nirvana": [make_candidate(1, "Nevermind", "Nirvana (2)", ("Rock",), ("Grunge",))],
    "Aphex Twin": [
        make_candidate(2, "Selected Ambient Works 85-92", "Aphex Twin", ("Electronic",), ("Ambient", "IDM"))
    ],
    "Boards Of Canada": [
        make_candidate(3, "Music Has The Right To Children", "Boards Of Canada", ("Electronic",), ("IDM", "Ambient"))
    ],
    "Miles Davis": [make_candidate(4, "Kind Of Blue", "Miles Davis", ("Jazz",), ("Modal",))],
}

LIBRARY = [
    Track("g1", "Come As You Are", ("Nirvana",), "Nevermind"),
    Track("g2", "Lithium", ("Nirvana",), "Nevermind"),
    Track("e1", "Xtal", ("Aphex Twin",), "Selected Ambient Works 85-92"),
    Track("e2", "Ageispolis", ("Aphex Twin",), "Selected Ambient Works 85-92"),
    Track("e3", "Roygbiv", ("Boards Of Canada",), "Music Has The Right To Children"),
    Track("j1", "So What", ("Miles Davis",), "Kind Of Blue"),
]


def run_pipeline(repository, config, client):
    repository.upsert_tracks(LIBRARY)
    classify_tracks(LIBRARY, repository, FakeDiscogs(CATALOGUE), config)
    plans = plan_playlists(repository.classifications(), load_taxonomy(), config)
    remote = managed_by_key(client.list_playlists(), config.sync.marker)
    actions = diff(plans, remote, config)
    apply(actions, plans, client, config, dry_run=False, on_created=repository.remember_playlist)
    return plans


def playlists_by_title(client):
    return {p.title: p for p in client.list_playlists()}


def test_full_run_creates_the_expected_playlists(repository, config):
    client = FakePlaylistClient()
    run_pipeline(repository, config, client)

    titles = playlists_by_title(client)
    # Ambient (3 titres) dépasse le seuil de style ; Grunge (2) aussi.
    assert set(titles) == {"Electronic — Ambient", "Rock — Grunge", "Divers"}
    assert set(titles["Electronic — Ambient"].video_ids) == {"e1", "e2", "e3"}
    assert titles["Rock — Grunge"].video_ids == ("g1", "g2")
    # Jazz n'a qu'un titre : sous les deux seuils, il finit au fourre-tout.
    assert titles["Divers"].video_ids == ("j1",)


def test_managed_playlists_are_recorded_for_the_next_run(repository, config):
    client = FakePlaylistClient()
    run_pipeline(repository, config, client)
    assert set(repository.managed_playlists()) == {"electronic/ambient", "rock/grunge", "divers"}


def test_second_run_is_idempotent(repository, config):
    client = FakePlaylistClient()
    run_pipeline(repository, config, client)
    before = {p.playlist_id: p.video_ids for p in client.list_playlists()}

    plans = plan_playlists(repository.classifications(), load_taxonomy(), config)
    remote = managed_by_key(client.list_playlists(), config.sync.marker)
    assert diff(plans, remote, config) == []
    assert {p.playlist_id: p.video_ids for p in client.list_playlists()} == before


def test_a_new_track_is_added_without_recreating_the_playlist(repository, config):
    client = FakePlaylistClient()
    run_pipeline(repository, config, client)
    playlist_id = playlists_by_title(client)["Rock — Grunge"].playlist_id

    extra = Track("g3", "In Bloom", ("Nirvana",), "Nevermind")
    repository.upsert_tracks([extra])
    classify_tracks([extra], repository, FakeDiscogs(CATALOGUE), config)
    plans = plan_playlists(repository.classifications(), load_taxonomy(), config)
    remote = managed_by_key(client.list_playlists(), config.sync.marker)
    apply(diff(plans, remote, config), plans, client, config, dry_run=False)

    assert client.get_playlist(playlist_id).video_ids == ("g1", "g2", "g3")


def test_user_playlists_are_left_untouched(repository, config):
    """Le contrat le plus important : l'outil ne touche pas au travail manuel."""
    perso = RemotePlaylist("PLperso", "Mes classiques", "sélection maison", ("g1", "j1"))
    client = FakePlaylistClient([perso])
    run_pipeline(repository, config, client)
    assert client.get_playlist("PLperso") == perso


def test_taxonomy_can_be_replanned_without_new_api_calls(repository, config):
    """Changer un seuil ne doit pas relancer une seule requête Discogs."""
    source = FakeDiscogs(CATALOGUE)
    repository.upsert_tracks(LIBRARY)
    classify_tracks(LIBRARY, repository, source, config)
    calls = len(source.searches)

    config.taxonomy.min_tracks_per_style = 99  # tous les styles se replient
    plans = plan_playlists(repository.classifications(), load_taxonomy(), config)

    assert len(source.searches) == calls
    assert {p.name for p in plans} == {"Electronic", "Rock", "Divers"}
