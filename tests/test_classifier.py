from conftest import make_candidate, make_track
from fakes import FakeDiscogs

from ytmgc.classifier import classify_tracks
from ytmgc.models import MatchStatus


def test_matched_track_stores_raw_discogs_taxonomy(repository, config):
    """La taxonomie est stockée brute : changer les alias ne recoûte aucun appel."""
    track = make_track("v1", "Come As You Are", "Nirvana", album="Nevermind")
    repository.upsert_tracks([track])
    source = FakeDiscogs({"Nirvana": [make_candidate(1, "Nevermind", "Nirvana (2)")]})

    stats = classify_tracks([track], repository, source, config)

    stored = repository.classifications()[0]
    assert stats.matched == 1
    assert stored.status is MatchStatus.MATCHED
    assert stored.genres == ("Rock",) and stored.styles == ("Grunge",)


def test_low_score_lands_in_review_not_in_playlists(repository, config):
    """Bon artiste, mauvais album : plausible, mais pas assez sûr pour classer."""
    track = make_track("v1", "La femme d'argent", "Air", album="Moon Safari")
    repository.upsert_tracks([track])
    source = FakeDiscogs(
        {"Air": [make_candidate(1, "Talkie Walkie", "Air", ("Electronic",), ("Downtempo",))]}
    )

    classify_tracks([track], repository, source, config)

    stored = repository.classifications()[0]
    assert stored.status is MatchStatus.REVIEW
    assert config.matching.review_score <= stored.score < config.matching.min_score


def test_no_candidate_is_recorded_as_unmatched(repository, config):
    track = make_track("v1", "T", "Inconnu")
    repository.upsert_tracks([track])
    stats = classify_tracks([track], repository, FakeDiscogs({}), config)
    assert stats.unmatched == 1
    assert repository.classifications()[0].status is MatchStatus.UNMATCHED


def test_tracks_from_the_same_album_share_one_api_call(repository, config):
    """C'est le levier principal face à la limite de 60 requêtes/minute."""
    tracks = [
        make_track("v1", "Come As You Are", "Nirvana", album="Nevermind"),
        make_track("v2", "Lithium", "Nirvana", album="Nevermind"),
        make_track("v3", "In Bloom", "Nirvana", album="Nevermind"),
    ]
    repository.upsert_tracks(tracks)
    source = FakeDiscogs({"Nirvana": [make_candidate(1, "Nevermind", "Nirvana (2)")]})

    stats = classify_tracks(tracks, repository, source, config)

    assert len(source.searches) == 1
    assert stats.api_calls == 1 and stats.from_cache == 2
    assert stats.matched == 3


def test_a_second_run_is_served_entirely_from_cache(repository, config):
    track = make_track("v1", "Come As You Are", "Nirvana", album="Nevermind")
    repository.upsert_tracks([track])
    source = FakeDiscogs({"Nirvana": [make_candidate(1, "Nevermind", "Nirvana (2)")]})

    classify_tracks([track], repository, source, config)
    stats = classify_tracks([track], repository, source, config)

    assert len(source.searches) == 1
    assert stats.from_cache == 1
