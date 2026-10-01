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


# ------------------------------------------------------- identité d'un morceau

from ytmgc.matching.normalize import song_key  # noqa: E402

#: Un même morceau tel que YouTube Music le présente réellement.
TEEN_SPIRIT = [
    ("Nirvana", "Smells Like Teen Spirit"),
    ("Nirvana", "Smells Like Teen Spirit (Official Music Video)"),
    ("Nirvana", "Smells Like Teen Spirit (Official HD Video)"),
    ("Nirvana", "Smells Like Teen Spirit [Official Video]"),
    ("Nirvana", "Smells Like Teen Spirit (Official Audio)"),
    ("Nirvana", "Smells Like Teen Spirit (Clip officiel)"),
    ("Nirvana", "Smells Like Teen Spirit (Visualizer)"),
    ("Nirvana", "Smells Like Teen Spirit (Single Version)"),
    ("Nirvana", "Smells Like Teen Spirit - Remastered Version"),
    ("Nirvana", "Smells Like Teen Spirit - 2011 Remaster"),
    ("NirvanaVEVO", "Smells Like Teen Spirit"),
    ("Nirvana - Topic", "Smells Like Teen Spirit"),
    ("Nirvana", "Nirvana - Smells Like Teen Spirit"),
    ("NirvanaVEVO", "Nirvana - Smells Like Teen Spirit (Official Music Video) [HD]"),
]


def test_every_publication_of_a_song_shares_one_key():
    """Régression : huit de ces variantes passaient pour des morceaux
    différents, et auraient été jugées — et payées — chacune à part."""
    assert {song_key(artist, title) for artist, title in TEEN_SPIRIT} == {
        "nirvana::smells like teen spirit"
    }


def test_versions_that_sound_different_keep_their_own_key():
    """Un live, un remix, une version acoustique peuvent ne pas avoir
    l'ambiance de l'original : ce sont d'autres morceaux pour l'outil."""
    original = song_key("Nirvana", "Smells Like Teen Spirit")
    for title in (
        "Smells Like Teen Spirit (Live at Reading)",
        "Smells Like Teen Spirit (Butch Vig Mix)",
        "Smells Like Teen Spirit (Acoustic Version)",
        "Smells Like Teen Spirit - Live",
    ):
        assert song_key("Nirvana", title) != original, title


def test_a_title_prefix_is_dropped_only_when_it_names_the_artist():
    """« Song 2 - Live » ne commence pas par l'artiste : rien à retirer."""
    assert song_key("Blur", "Song 2 - Live") == "blur::song 2 live"
    assert song_key("Blur", "Blur - Song 2") == "blur::song 2"


def test_a_song_named_like_a_noise_word_survives():
    """« Stereo » est un titre de Muse, pas une mention de mixage."""
    assert song_key("Muse", "Stereo") == "muse::stereo"
    assert strip_noise("Mono") == "Mono"


def test_a_noise_word_inside_a_real_word_is_left_alone():
    assert strip_noise("Shadow (Monologue)") == "Shadow (Monologue)"


def test_youtube_channel_suffixes_are_not_part_of_the_artist():
    assert normalize_artist("NirvanaVEVO") == "nirvana"
    assert normalize_artist("Nirvana - Topic") == "nirvana"
