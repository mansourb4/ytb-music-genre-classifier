from ytmgc.taxonomy import load_taxonomy
from ytmgc.taxonomy.rules import GenreStyle, playlist_name


def test_style_aliases_only_merge_spelling_variants():
    taxonomy = load_taxonomy()
    assert taxonomy.canonical_style("Drum n Bass") == "Drum & Bass"
    assert taxonomy.canonical_style("Prog Rock") == "Progressive Rock"


def test_distinct_styles_are_never_merged():
    """Jungle et Drum & Bass sont deux styles : les confondre effacerait
    exactement l'information que le projet cherche à restituer."""
    taxonomy = load_taxonomy()
    assert taxonomy.canonical_style("Jungle") == "Jungle"
    assert taxonomy.canonical_style("Indie Rock") == "Indie Rock"
    assert taxonomy.canonical_style("Minimal Techno") == "Minimal Techno"


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


def test_style_homonym_of_its_genre_does_not_duplicate_the_name():
    """Évite les playlists « Reggae — Reggae »."""
    taxonomy = load_taxonomy()
    assert taxonomy.resolve(("Reggae",), ("Reggae",)) == (GenreStyle("Reggae", None),)
    assert taxonomy.resolve(("Reggae",), ("Reggae", "Dub")) == (GenreStyle("Reggae", "Dub"),)


def test_hip_hop_substyles_are_preserved():
    """Boom Bap est un style à part entière : le fusionner effaçait l'information."""
    assert load_taxonomy().canonical_style("Boom Bap") == "Boom Bap"


def test_every_prioritised_genre_has_a_description():
    """Un genre sans définition produirait une playlist muette."""
    import tomllib
    from pathlib import Path

    from ytmgc.taxonomy.rules import DEFAULT_ALIASES

    taxonomy = load_taxonomy()
    order = tomllib.loads(Path(DEFAULT_ALIASES).read_text(encoding="utf-8"))["genre_priority"]["order"]
    assert [genre for genre in order if taxonomy.describe_genre(genre) is None] == []


def test_genre_description_is_reachable_by_display_name():
    taxonomy = load_taxonomy()
    assert taxonomy.describe_genre("Funk / Soul") == taxonomy.describe_genre("Funk & Soul")


def test_common_styles_are_described():
    taxonomy = load_taxonomy()
    for style in ["Deep House", "Post-Punk", "Boom Bap", "Hard Bop", "Dub", "Bossa Nova"]:
        assert taxonomy.describe_style(style), style


def test_an_undescribed_style_is_not_an_error():
    """Discogs ajoute régulièrement des styles : l'absence doit être tolérée."""
    assert load_taxonomy().describe_style("Style Inventé Demain") is None


def test_aliased_style_description_is_found_under_its_canonical_name():
    taxonomy = load_taxonomy()
    assert taxonomy.describe_style(taxonomy.canonical_style("Prog Rock"))
