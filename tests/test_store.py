from conftest import make_candidate, make_track

from ytmgc.models import Classification, MatchStatus


def test_upsert_is_idempotent_and_updates_metadata(repository):
    repository.upsert_tracks([make_track("v1", "Ancien titre", "A")])
    repository.upsert_tracks([make_track("v1", "Nouveau titre", "A", album="Al")])
    tracks = repository.all_tracks()
    assert len(tracks) == 1
    assert tracks[0].title == "Nouveau titre"
    assert tracks[0].album == "Al"


def test_multiple_artists_survive_a_round_trip(repository):
    from ytmgc.models import Track

    repository.upsert_tracks([Track("v1", "T", ("Air", "Beth Hirsch"))])
    assert repository.all_tracks()[0].artists == ("Air", "Beth Hirsch")


def test_unclassified_tracks_enable_resuming_an_interrupted_run(repository):
    repository.upsert_tracks([make_track("v1", "T1", "A"), make_track("v2", "T2", "B")])
    repository.save_classification(Classification("v1", MatchStatus.MATCHED, 1, 0.9))
    assert [t.video_id for t in repository.unclassified_tracks()] == ["v2"]


def test_candidate_cache_round_trip(repository):
    candidates = [make_candidate(1, "Nevermind", "Nirvana")]
    repository.store_candidates("nirvana::nevermind", candidates)
    assert repository.cached_candidates("nirvana::nevermind", 90) == candidates


def test_expired_and_missing_cache_entries_return_none(repository):
    repository.store_candidates("k", [make_candidate(1, "T", "A")])
    assert repository.cached_candidates("k", 0) is None
    assert repository.cached_candidates("absent", 90) is None


def test_classification_is_upserted_not_duplicated(repository):
    repository.upsert_tracks([make_track("v1", "T", "A")])
    repository.save_classification(Classification("v1", MatchStatus.REVIEW, 1, 0.6))
    repository.save_classification(Classification("v1", MatchStatus.MATCHED, 2, 0.9, ("Rock",)))
    stored = repository.classifications()
    assert len(stored) == 1
    assert stored[0].status is MatchStatus.MATCHED
    assert stored[0].genres == ("Rock",)


def test_counts_by_status(repository):
    repository.upsert_tracks([make_track(f"v{i}", "T", "A") for i in range(3)])
    repository.save_classification(Classification("v0", MatchStatus.MATCHED, 1, 0.9))
    repository.save_classification(Classification("v1", MatchStatus.MATCHED, 2, 0.9))
    repository.save_classification(Classification("v2", MatchStatus.UNMATCHED))
    assert repository.counts_by_status() == {"matched": 2, "unmatched": 1}


def test_managed_playlists_are_remembered_by_key(repository):
    repository.remember_playlist("rock/grunge", "PL1", "Rock — Grunge")
    repository.remember_playlist("rock/grunge", "PL2", "Rock — Grunge")
    assert repository.managed_playlists() == {"rock/grunge": ("PL2", "Rock — Grunge")}
