import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from ytmgc.config import Config
from ytmgc.models import ReleaseCandidate, Track
from ytmgc.store import Repository, connect


@pytest.fixture
def config() -> Config:
    """Configuration avec des seuils bas, adaptés à des jeux de test courts."""
    config = Config()
    config.taxonomy.min_tracks_per_style = 2
    config.taxonomy.min_tracks_per_genre = 2
    config.validate()
    return config


@pytest.fixture
def repository() -> Repository:
    return Repository(connect(":memory:"))


def make_track(video_id: str, title: str, artist: str, album: str | None = None) -> Track:
    return Track(video_id=video_id, title=title, artists=(artist,), album=album)


def make_candidate(
    discogs_id: int,
    title: str,
    artist: str,
    genres: tuple[str, ...] = ("Rock",),
    styles: tuple[str, ...] = ("Grunge",),
    year: int | None = 1991,
) -> ReleaseCandidate:
    return ReleaseCandidate(
        discogs_id=discogs_id, title=title, artist=artist, year=year, genres=genres, styles=styles
    )
