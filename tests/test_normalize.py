from ytmgc.matching.normalize import (
    fold,
    normalize_artist,
    normalize_title,
    slugify,
    split_artists,
    split_discogs_title,
    strip_noise,
)


def test_fold_removes_accents_and_punctuation():
    assert fold("Édith Piaf — Non, je ne regrette rien!") == "edith piaf non je ne regrette rien"


def test_strip_noise_drops_editorial_segments():
    assert strip_noise("Smells Like Teen Spirit (Official Music Video)") == "Smells Like Teen Spirit"
    assert strip_noise("Come As You Are - Remastered 2011") == "Come As You Are"


def test_strip_noise_keeps_meaningful_parentheses():
    """Une mention de version distingue deux enregistrements : on la conserve."""
    assert strip_noise("Voodoo People (Chemical Brothers Remix)").endswith("(Chemical Brothers Remix)")
    assert "Live" in strip_noise("Creep (Live At Glastonbury)")


def test_feat_pattern_does_not_bite_into_artist_names():
    """Régression : sans limite de mot, "ft" ampute "Daft Punk"."""
    assert split_artists("Daft Punk feat. Pharrell Williams") == ("daft punk",)
    assert normalize_artist("Left Boy") == "left boy"


def test_with_is_not_treated_as_a_featuring_marker():
    assert normalize_title("Dancing With Myself") == "dancing with myself"


def test_split_artists_handles_separators_and_dedupes():
    assert split_artists("Simon & Garfunkel") == ("simon", "garfunkel")
    assert split_artists(["Air", "Air"]) == ("air",)


def test_normalize_artist_strips_discogs_homonym_suffix_and_leading_the():
    assert normalize_artist("Nirvana (2)") == "nirvana"
    assert normalize_artist("The Beatles") == "beatles"


def test_split_discogs_title_splits_on_first_separator_only():
    assert split_discogs_title("Aphex Twin - Selected Ambient Works 85-92") == (
        "Aphex Twin",
        "Selected Ambient Works 85-92",
    )
    assert split_discogs_title("Nevermind") == ("", "Nevermind")


def test_slugify_is_stable_and_never_empty():
    assert slugify("Drum & Bass") == "drum-and-bass"
    assert slugify("!!!") == "inconnu"
