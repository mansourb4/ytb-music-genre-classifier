import pytest

from ytmgc.config import Config, load_config


def test_example_config_matches_the_dataclasses():
    """Le fichier d'exemple doit rester valide : c'est la doc de référence."""
    from pathlib import Path

    config = load_config(Path("config/config.example.toml"), env={})
    assert config.taxonomy.multi_style == "all"
    assert config.sync.marker == "✱"


def test_secrets_come_from_the_environment_only():
    config = load_config(None, env={"DISCOGS_TOKEN": "secret", "YTMUSIC_AUTH_FILE": "oauth.json"})
    assert config.discogs.token == "secret"
    assert config.youtube.auth_file == "oauth.json"


def test_unknown_keys_are_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[sync]\nmarqueur = 'x'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sync.marqueur"):
        load_config(path, env={})


def test_unknown_section_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[inconnu]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inconnu"):
        load_config(path, env={})


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda c: setattr(c.taxonomy, "multi_style", "both"), "multi_style"),
        (lambda c: setattr(c.matching, "min_score", 1.5), "min_score"),
        (lambda c: setattr(c.matching, "review_score", 0.99), "review_score"),
        (lambda c: setattr(c.youtube, "playlist_privacy", "SECRET"), "playlist_privacy"),
        (lambda c: setattr(c.sync, "marker", ""), "marker"),
    ],
)
def test_validation_rejects_inconsistent_settings(mutate, message):
    config = Config()
    mutate(config)
    with pytest.raises(ValueError, match=message):
        config.validate()
