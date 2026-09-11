"""Modes de tri : ce sont eux qui décident du résultat final."""

import pytest

from ytmgc.config import Config
from ytmgc.sorting import SORT_MODES, apply_sort_mode, get_sort_mode


def test_every_mode_is_described_for_the_interface():
    for mode in SORT_MODES:
        assert mode.label and mode.summary and mode.describe()


def test_an_unknown_mode_names_the_valid_ones():
    with pytest.raises(ValueError, match="detaille"):
        get_sort_mode("n-importe-quoi")


def test_applying_a_mode_leaves_the_original_untouched():
    """L'interface essaie plusieurs modes de suite sur une même session."""
    config = Config()
    apply_sort_mode(config, "genre")
    assert config.taxonomy.min_tracks_per_style == 4


def test_the_mood_mode_switches_axis_and_vocabulary():
    scoped = apply_sort_mode(Config(), "ambiance")
    assert scoped.taxonomy.axis == "mood"
    assert scoped.taxonomy.fallback_playlist == "Ambiance — Non déterminée"
    assert "ambiance" in get_sort_mode("ambiance").describe()


def test_the_other_modes_keep_the_discogs_axis():
    for key in ("detaille", "exhaustif", "sans-doublon", "genre"):
        assert apply_sort_mode(Config(), key).taxonomy.axis == "style"


def test_an_invalid_axis_is_refused():
    config = Config()
    config.taxonomy.axis = "humeur"
    with pytest.raises(ValueError, match="axis"):
        config.validate()
