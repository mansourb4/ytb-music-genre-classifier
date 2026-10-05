"""Playlists exclues : leurs titres sortent de l'analyse, d'où qu'ils viennent."""

import pytest
from conftest import make_candidate
from fakes import FakeDiscogs, FakePlaylistClient

from ytmgc import cli
from ytmgc.exclusions import apply_exclusions
from ytmgc.models import Track
from ytmgc.store import Repository, connect
from ytmgc.web.app import Services, create_app
from ytmgc.web.jobs import JobRunner

fastapi_testclient = pytest.importorskip("fastapi.testclient")

LULLABY = Track("k1", "Une souris verte", ("Comptines",), "Berceuses")
LULLABY_CLIP = Track("k2", "Une souris verte (Clip officiel)", ("ComptinesVEVO",))
ROCK = Track("r1", "Lithium", ("Nirvana",), "Nevermind")


class Library(FakePlaylistClient):
    """La bibliothèque contient tout ; la playlist « Berceuses » en reprend une partie."""

    playlists = {"PLdodo": [LULLABY]}

    def scan(self, sources):
        tracks = []
        for source in sources:
            if source.startswith("playlist:"):
                tracks += self.playlists.get(source.split(":", 1)[1], [])
            else:
                tracks += [LULLABY, LULLABY_CLIP, ROCK]
        return list({track.video_id: track for track in tracks}.values())

    def list_playlist_summaries(self):
        return [{"playlist_id": "PLdodo", "title": "Berceuses"}]


# ------------------------------------------------------------------ règle


def test_a_track_of_an_excluded_playlist_is_removed():
    scope = apply_exclusions([LULLABY, ROCK], [LULLABY])
    assert scope.kept == [ROCK] and scope.removed == [LULLABY]


def test_the_same_song_published_otherwise_is_removed_too():
    """Exclure la comptine doit aussi écarter son clip, arrivé par les likes."""
    scope = apply_exclusions([LULLABY_CLIP, ROCK], [LULLABY])
    assert scope.kept == [ROCK]


def test_nothing_excluded_keeps_everything():
    assert apply_exclusions([LULLABY, ROCK], []).kept == [LULLABY, ROCK]


# -------------------------------------------------------------- stockage


def test_exclusions_survive_the_library_reset(repository):
    """Chaque analyse vide la bibliothèque : une exclusion, elle, est durable."""
    repository.set_excluded_playlists(["PLdodo"])
    repository.clear_library()
    assert repository.excluded_playlists() == ["PLdodo"]


# ------------------------------------------------------------------- web


@pytest.fixture
def client(repository, config):
    services = Services(
        config=config,
        repository=repository,
        youtube_factory=lambda _c: Library(),
        discogs_factory=lambda _c: FakeDiscogs(
            {"Nirvana": [make_candidate(1, "Nevermind", "Nirvana")]}),
        jobs=JobRunner(),
    )
    return fastapi_testclient.TestClient(create_app(services))


def wait(client, response):
    job_id = response.json()["id"]
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "en cours":
            return job
    raise AssertionError("Le traitement ne se termine pas")


def test_exclusions_are_saved_and_listed_with_the_sources(client, repository):
    response = client.put("/api/exclusions", json={"playlists": ["PLdodo", "PLdodo"]})

    assert response.json() == {"excluded": ["PLdodo"]}
    assert client.get("/api/sources").json()["excluded"] == ["PLdodo"]


def test_the_analysis_leaves_out_excluded_playlists(client, repository):
    client.put("/api/exclusions", json={"playlists": ["PLdodo"]})
    job = wait(client, client.post("/api/analyse", json={"sources": ["library"]}))

    assert job["status"] == "terminé", job["error"]
    assert [t.video_id for t in repository.all_tracks()] == ["r1"]
    assert job["result"]["excluded"] == 2
    assert "retiré(s) par les playlists exclues" in job["result"]["summary"]


def test_without_exclusions_everything_is_analysed(client, repository):
    wait(client, client.post("/api/analyse", json={"sources": ["library"]}))
    assert len(repository.all_tracks()) == 3


def test_lifting_an_exclusion_brings_the_tracks_back(client, repository):
    client.put("/api/exclusions", json={"playlists": ["PLdodo"]})
    wait(client, client.post("/api/analyse", json={"sources": ["library"]}))
    client.put("/api/exclusions", json={"playlists": []})
    wait(client, client.post("/api/analyse", json={"sources": ["library"]}))

    assert len(repository.all_tracks()) == 3


# ------------------------------------------------------------------- CLI


def test_scan_applies_the_saved_exclusions(config, monkeypatch, tmp_path, capsys):
    config.store.path = str(tmp_path / "cli.db")
    config.youtube.sources = ["library"]
    repository = Repository(connect(config.store.path))
    # Un scan antérieur à l'exclusion a laissé la berceuse en base.
    repository.upsert_tracks([LULLABY])
    repository.set_excluded_playlists(["PLdodo"])
    monkeypatch.setattr(cli, "_youtube", lambda _config: Library())
    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    assert cli.main(["scan"]) == 0
    assert [t.video_id for t in repository.all_tracks()] == ["r1"]
    assert "2 titre(s) retiré(s) par 1 playlist(s) exclue(s)" in capsys.readouterr().out
