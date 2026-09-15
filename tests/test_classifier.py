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


# ------------------------------------------- affinage par les tags Last.fm


class FakeTags:
    """Tags par titre, avec leur poids."""

    def __init__(self, by_title):
        self._by_title = by_title
        self.asked = []

    def top_tags(self, track):
        self.asked.append(track.title)
        return list(self._by_title.get(track.title, []))


PUNK_ALBUM = {"Sex Pistols": [make_candidate(1, "Bollocks", "Sex Pistols", ("Rock",), ("Punk",))]}


def punk_library():
    return [
        make_track("v1", "Anarchy", "Sex Pistols", album="Bollocks"),
        make_track("v2", "La ballade", "Sex Pistols", album="Bollocks"),
    ]


def test_a_ballad_on_a_punk_album_keeps_its_own_style(repository, config):
    """Le cas qui motive toute cette source : Discogs étiquette le disque,
    Last.fm le morceau."""
    library = punk_library()
    repository.upsert_tracks(library)
    tags = FakeTags({"La ballade": [("ballad", 90), ("acoustic", 70)]})

    classify_tracks(library, repository, FakeDiscogs(PUNK_ALBUM), config, tag_source=tags)

    stored = {c.video_id: c for c in repository.classifications()}
    assert stored["v1"].styles == ("Punk",)          # aucun tag : la release fait foi
    assert stored["v2"].styles == ("Ballad",)        # ses propres tags priment


def test_the_ballad_lands_in_a_different_mood(repository, config):
    from ytmgc.planner import plan_playlists
    from ytmgc.sorting import apply_sort_mode
    from ytmgc.taxonomy import load_taxonomy

    library = punk_library()
    repository.upsert_tracks(library)
    tags = FakeTags({"La ballade": [("mellow", 95)], "Anarchy": [("aggressive", 95)]})
    classify_tracks(library, repository, FakeDiscogs(PUNK_ALBUM), config, tag_source=tags)

    scoped = apply_sort_mode(config, "ambiance")
    scoped.taxonomy.min_tracks_per_style = 1
    scoped.taxonomy.min_tracks_per_genre = 1
    by_name = {p.name: p.video_ids for p in plan_playlists(repository.classifications(),
                                                          load_taxonomy(), scoped)}
    assert by_name["Ambiance — Calme"] == ("v2",)
    assert by_name["Ambiance — Sombre"] == ("v1",)


def test_free_form_tags_are_ignored(repository, config):
    """« seen live » ou « 00s » ne nomment aucun style : rien à filtrer à la main."""
    library = punk_library()
    repository.upsert_tracks(library)
    tags = FakeTags({"La ballade": [("seen live", 100), ("00s", 90), ("favourites", 80)]})

    classify_tracks(library, repository, FakeDiscogs(PUNK_ALBUM), config, tag_source=tags)

    stored = {c.video_id: c for c in repository.classifications()}
    assert stored["v2"].styles == ("Punk",)
    assert "seen live" in stored["v2"].tags  # conservés pour l'affichage


def test_a_weakly_posed_tag_does_not_override_the_release(repository, config):
    library = punk_library()
    repository.upsert_tracks(library)
    tags = FakeTags({"La ballade": [("ballad", 3)]})

    classify_tracks(library, repository, FakeDiscogs(PUNK_ALBUM), config, tag_source=tags)

    assert {c.video_id: c.styles for c in repository.classifications()}["v2"] == ("Punk",)


def test_tags_are_fetched_once_then_cached(repository, config):
    library = punk_library()
    repository.upsert_tracks(library)
    tags = FakeTags({"La ballade": [("ballad", 90)]})

    classify_tracks(library, repository, FakeDiscogs(PUNK_ALBUM), config, tag_source=tags)
    classify_tracks(library, repository, FakeDiscogs(PUNK_ALBUM), config, tag_source=tags)

    assert sorted(tags.asked) == ["Anarchy", "La ballade"]


def test_a_failing_tag_source_never_stops_the_analysis(repository, config):
    class Broken:
        def top_tags(self, track):
            raise RuntimeError("Last.fm indisponible")

    library = punk_library()
    repository.upsert_tracks(library)
    stats = classify_tracks(library, repository, FakeDiscogs(PUNK_ALBUM), config, tag_source=Broken())
    assert stats.matched == 2


def test_without_a_tag_source_nothing_changes(repository, config):
    library = punk_library()
    repository.upsert_tracks(library)
    classify_tracks(library, repository, FakeDiscogs(PUNK_ALBUM), config)
    assert all(c.styles == ("Punk",) for c in repository.classifications())
