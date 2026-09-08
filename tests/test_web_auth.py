"""Contrôle d'accès : l'interface ne doit jamais être ouverte hors boucle locale."""

import pytest
from fakes import FakeDiscogs, FakePlaylistClient

from ytmgc.web.app import Services, create_app
from ytmgc.web.jobs import JobRunner
from ytmgc.web.security import COOKIE_NAME, HEADER_NAME, generate_token

fastapi_testclient = pytest.importorskip("fastapi.testclient")

TOKEN = "jeton-de-test-0123456789"


def build(config, repository, **web):
    for key, value in web.items():
        setattr(config.web, key, value)
    return fastapi_testclient.TestClient(
        create_app(
            Services(
                config=config,
                repository=repository,
                youtube_factory=lambda _c: FakePlaylistClient(),
                discogs_factory=lambda _c: FakeDiscogs({}),
                jobs=JobRunner(),
            )
        )
    )


def test_loopback_needs_no_token(config, repository):
    """En local, l'isolement réseau suffit : pas de friction inutile."""
    client = build(config, repository, host="127.0.0.1")
    assert client.get("/api/status").status_code == 200


def test_exposed_without_token_refuses_to_start(config, repository):
    """Plutôt qu'un service ouvert, un refus explicite au démarrage."""
    with pytest.raises(ValueError, match="jeton"):
        build(config, repository, host="0.0.0.0", access_token="")


def test_exposed_rejects_requests_without_a_token(config, repository):
    client = build(config, repository, host="0.0.0.0", access_token=TOKEN)
    for path in ["/", "/api/status", "/api/sort-modes"]:
        assert client.get(path).status_code == 401, path


def test_write_routes_are_protected_too(config, repository):
    client = build(config, repository, host="0.0.0.0", access_token=TOKEN)
    assert client.post("/api/apply", json={"confirm": True}).status_code == 401
    assert client.post("/api/purge", json={"confirm": True}).status_code == 401


def test_a_wrong_token_is_rejected(config, repository):
    client = build(config, repository, host="0.0.0.0", access_token=TOKEN)
    assert client.get("/api/status", headers={HEADER_NAME: "presque-le-bon"}).status_code == 401


def test_header_grants_access(config, repository):
    client = build(config, repository, host="0.0.0.0", access_token=TOKEN)
    assert client.get("/api/status", headers={HEADER_NAME: TOKEN}).status_code == 200


def test_query_parameter_grants_access_and_sets_a_cookie(config, repository):
    """Le lien à ouvrir sur un téléphone porte le jeton ; ensuite le cookie suffit."""
    client = build(config, repository, host="0.0.0.0", access_token=TOKEN)

    first = client.get(f"/api/status?token={TOKEN}")
    assert first.status_code == 200
    assert client.cookies.get(COOKIE_NAME) == TOKEN

    assert client.get("/api/status").status_code == 200


def test_cookie_is_not_readable_by_scripts(config, repository):
    client = build(config, repository, host="0.0.0.0", access_token=TOKEN)
    response = client.get(f"/?token={TOKEN}")
    assert "httponly" in response.headers["set-cookie"].lower()


def test_token_can_be_disabled_deliberately(config, repository):
    """Échappatoire assumée pour un accès déjà protégé par ailleurs."""
    client = build(config, repository, host="0.0.0.0", require_token=False)
    assert client.get("/api/status").status_code == 200


def test_generated_tokens_are_unique_and_long_enough():
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(token) >= 32 for token in tokens)
