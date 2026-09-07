import pytest

from ytmgc.models import Classification, MatchStatus
from ytmgc.planner import plan_playlists
from ytmgc.taxonomy import load_taxonomy


def matched(video_id, genres, styles):
    return Classification(video_id, MatchStatus.MATCHED, 1, 0.9, genres, styles)


@pytest.fixture
def taxonomy():
    return load_taxonomy()


def plan(classifications, taxonomy, config):
    return {p.key: p for p in plan_playlists(classifications, taxonomy, config)}


def test_style_playlist_created_above_threshold(taxonomy, config):
    plans = plan(
        [matched(f"v{i}", ("Electronic",), ("Techno",)) for i in range(2)], taxonomy, config
    )
    assert plans["electronic/techno"].name == "Electronic — Techno"
    assert plans["electronic/techno"].kind == "style"


def test_rare_style_folds_into_its_genre_playlist(taxonomy, config):
    """Un style à un seul titre ne doit pas créer une playlist de un titre."""
    classifications = [matched("v0", ("Rock",), ("Grunge",)), matched("v1", ("Rock",), ("Shoegaze",))]
    plans = plan(classifications, taxonomy, config)
    assert set(plans) == {"rock"}
    assert plans["rock"].kind == "genre"
    assert plans["rock"].video_ids == ("v0", "v1")


def test_rare_genre_folds_into_the_catch_all_playlist(taxonomy, config):
    plans = plan([matched("v0", ("Jazz",), ("Bebop",))], taxonomy, config)
    assert set(plans) == {"divers-genres-isoles"}
    assert plans["divers-genres-isoles"].kind == "fallback"


def test_unmatched_tracks_never_reach_a_playlist(taxonomy, config):
    classifications = [
        matched("v0", ("Electronic",), ("Techno",)),
        matched("v1", ("Electronic",), ("Techno",)),
        Classification("v2", MatchStatus.UNMATCHED),
        Classification("v3", MatchStatus.REVIEW, 1, 0.6, ("Electronic",), ("Techno",)),
    ]
    plans = plan(classifications, taxonomy, config)
    assert plans["electronic/techno"].video_ids == ("v0", "v1")


def test_primary_mode_assigns_each_track_once(taxonomy, config):
    classifications = [matched(f"v{i}", ("Electronic",), ("Deep House", "Tech House")) for i in range(3)]
    plans = plan(classifications, taxonomy, config)
    assert len(plans) == 1
    assert sum(len(p.video_ids) for p in plans.values()) == 3


def test_primary_mode_prefers_the_most_common_style_in_the_library(taxonomy, config):
    """Le titre ambigu rejoint la playlist déjà peuplée plutôt qu'une nouvelle."""
    classifications = [matched(f"v{i}", ("Electronic",), ("Techno",)) for i in range(3)]
    classifications.append(matched("amb", ("Electronic",), ("Ambient", "Techno")))
    plans = plan(classifications, taxonomy, config)
    assert "amb" in plans["electronic/techno"].video_ids


def test_all_mode_duplicates_the_track_across_styles(taxonomy, config):
    config.taxonomy.multi_style = "all"
    classifications = [matched(f"v{i}", ("Electronic",), ("Deep House", "Tech House")) for i in range(3)]
    plans = plan(classifications, taxonomy, config)
    assert set(plans) == {"electronic/deep-house", "electronic/tech-house"}


def test_all_mode_respects_max_styles_per_track(taxonomy, config):
    config.taxonomy.multi_style = "all"
    config.taxonomy.max_styles_per_track = 1
    classifications = [matched(f"v{i}", ("Electronic",), ("Deep House", "Tech House")) for i in range(3)]
    plans = plan(classifications, taxonomy, config)
    assert set(plans) == {"electronic/deep-house"}


def test_description_carries_marker_and_key(taxonomy, config):
    plans = plan([matched(f"v{i}", ("Electronic",), ("Techno",)) for i in range(2)], taxonomy, config)
    description = plans["electronic/techno"].description
    assert config.sync.marker in description
    assert "key=electronic/techno" in description


def test_plans_are_sorted_by_name_so_genres_group_together(taxonomy, config):
    classifications = [matched(f"e{i}", ("Electronic",), ("Techno",)) for i in range(2)]
    classifications += [matched(f"h{i}", ("Hip Hop",), ("Trip Hop",)) for i in range(2)]
    classifications += [matched(f"d{i}", ("Electronic",), ("Deep House",)) for i in range(2)]
    names = [p.name for p in plan_playlists(classifications, taxonomy, config)]
    assert names == ["Electronic — Deep House", "Electronic — Techno", "Hip Hop — Trip Hop"]


def test_track_joins_an_existing_style_playlist_rather_than_the_catch_all(taxonomy, config):
    """Le style élu n'a pas de playlist, mais un autre style du titre en a une."""
    classifications = [matched(f"a{i}", ("Electronic",), ("Ambient", "IDM")) for i in range(2)]
    classifications.append(matched("solo", ("Electronic",), ("Drone", "Ambient")))

    plans = plan(classifications, taxonomy, config)

    assert set(plans) == {"electronic/ambient"}
    assert "solo" in plans["electronic/ambient"].video_ids


def test_rescue_does_not_apply_when_no_style_survives(taxonomy, config):
    classifications = [matched("solo", ("Electronic",), ("Drone", "Ambient"))]
    plans = plan(classifications, taxonomy, config)
    assert set(plans) == {"divers-genres-isoles"}


def test_description_defines_the_genre_and_the_style(taxonomy, config):
    plans = plan([matched(f"v{i}", ("Electronic",), ("Deep House",)) for i in range(2)], taxonomy, config)
    description = plans["electronic/deep-house"].description

    assert "GENRE — Electronic" in description
    assert "STYLE — Deep House" in description
    assert taxonomy.describe_genre("Electronic") in description
    assert taxonomy.describe_style("Deep House") in description
    assert "2 titre(s)" in description


def test_genre_playlist_description_explains_why_it_exists(taxonomy, config):
    classifications = [matched("v0", ("Rock",), ("Grunge",)), matched("v1", ("Rock",), ("Shoegaze",))]
    description = plan(classifications, taxonomy, config)["rock"].description

    assert "GENRE — Rock" in description
    assert "STYLE" not in description
    assert "trop peu représenté" in description


def test_fallback_playlist_description_explains_why_it_exists(taxonomy, config):
    description = plan([matched("v0", ("Jazz",), ("Bebop",))], taxonomy, config)["divers-genres-isoles"].description
    assert "trop peu représentés" in description
    assert "GENRE" not in description


def test_description_contains_no_angle_brackets(taxonomy, config):
    """YouTube Music refuse les chevrons dans une description de playlist."""
    config.taxonomy.style_name_template = "{genre} <{style}>"
    plans = plan([matched(f"v{i}", ("Electronic",), ("Deep House",)) for i in range(2)], taxonomy, config)
    assert "<" not in plans["electronic/deep-house"].description
    assert ">" not in plans["electronic/deep-house"].description


def test_an_undescribed_style_still_produces_a_usable_description(taxonomy, config):
    plans = plan([matched(f"v{i}", ("Electronic",), ("Style Inventé",)) for i in range(2)], taxonomy, config)
    description = plans["electronic/style-invente"].description
    assert "STYLE — Style Inventé" in description
    assert "non encore renseignée" in description
