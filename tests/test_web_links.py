from ytmgc.web.links import access_url, in_codespace

CODESPACE = {
    "CODESPACE_NAME": "fluffy-space-pancake-abc123",
    "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev",
}


def test_listening_address_is_not_a_visitable_address():
    """Régression : « 0.0.0.0 » s'affichait dans un lien qu'on ne peut pas ouvrir."""
    assert access_url("0.0.0.0", 8765, env={}) == "http://127.0.0.1:8765"
    assert access_url("::", 8765, env={}) == "http://127.0.0.1:8765"


def test_explicit_host_is_preserved():
    assert access_url("192.168.1.42", 8765, env={}) == "http://192.168.1.42:8765"


def test_codespace_url_uses_the_forwarding_domain():
    url = access_url("0.0.0.0", 8765, env=CODESPACE)
    assert url == "https://fluffy-space-pancake-abc123-8765.app.github.dev"


def test_token_is_appended_so_the_link_works_as_is():
    assert access_url("0.0.0.0", 8765, "s3cr3t", env=CODESPACE).endswith("/?token=s3cr3t")
    assert access_url("0.0.0.0", 8765, "s3cr3t", env={}) == "http://127.0.0.1:8765/?token=s3cr3t"


def test_codespace_detection():
    assert in_codespace(CODESPACE) is True
    assert in_codespace({}) is False
    assert in_codespace({"CODESPACE_NAME": "x"}) is False
