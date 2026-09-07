from ytmgc.taxonomy import load_taxonomy
from ytmgc.taxonomy.rules import GenreStyle, playlist_name


def test_style_aliases_merge_variants():
    taxonomy = load_taxonomy()
    assert taxonomy.canonical_style("Drum n Bass") == "Drum & Bass"
    assert taxonomy.canonical_style("Jungle") == "Drum & Bass"


def test_generic_styles_are_dropped():
    assert load_taxonomy().canonical_style("Reissue") is None


def test_primary_genre_follows_configured_priority():
    """« Electronic, Pop » décrit mieux une playlist sous Electronic."""
    taxonomy = load_taxonomy()
    assert taxonomy.primary_genre(("Pop", "Electronic")) == "Electronic"
    assert taxonomy.primary_genre(("Rock", "Hip Hop")) == "Hip Hop"


def test_unknown_genre_is_kept_but_ranked_last():
    taxonomy = load_taxonomy()
    assert taxonomy.primary_genre(("Genre Inédit",)) == "Genre Inédit"
    assert taxonomy.primary_genre(("Genre Inédit", "Jazz")) == "Jazz"


def test_resolve_produces_display_names_and_dedupes():
    taxonomy = load_taxonomy()
    resolved = taxonomy.resolve(("Funk / Soul",), ("Disco", "Disco"))
    assert resolved == (GenreStyle("Funk & Soul", "Disco"),)


def test_resolve_falls_back_to_genre_only_when_all_styles_are_dropped():
    resolved = load_taxonomy().resolve(("Rock",), ("Reissue",))
    assert resolved == (GenreStyle("Rock", None),)


def test_resolve_returns_nothing_without_genre():
    assert load_taxonomy().resolve((), ("Techno",)) == ()


def test_playlist_name_encodes_the_hierarchy_in_the_title():
    """YouTube Music n'a pas de dossiers : le préfixe de genre tient ce rôle."""
    name = playlist_name(
        GenreStyle("Electronic", "Deep House"),
        style_template="{genre} — {style}",
        genre_template="{genre}",
    )
    assert name == "Electronic — Deep House"


def test_genre_style_key_is_slug_based():
    assert GenreStyle("Funk & Soul", "Drum & Bass").key == "funk-and-soul/drum-and-bass"
    assert GenreStyle("Rock", None).key == "rock"
