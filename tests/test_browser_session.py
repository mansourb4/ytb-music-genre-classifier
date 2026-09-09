"""Reprise de session : la voie sans aucune saisie."""

import json

import pytest

from ytmgc.sources.browser_session import (
    REQUIRED_COOKIE,
    BrowserSessionError,
    Session,
    build_headers,
    cookies_from_jar,
    import_session,
    read_session,
)

COOKIE = f"SID=abc; {REQUIRED_COOKIE}=signature; HSID=def"


class FakeCookie:
    def __init__(self, name, value, domain=".youtube.com"):
        self.name, self.value, self.domain = name, value, domain


def jar(*cookies):
    return list(cookies)


def reader_of(*cookies):
    return lambda _browser: jar(*cookies)


# ------------------------------------------------------------------ cookies


def test_only_youtube_cookies_are_kept():
    cookies = cookies_from_jar(
        jar(
            FakeCookie("SID", "a"),
            FakeCookie("session", "autre", domain=".example.com"),
            FakeCookie("PREF", "b", domain="music.youtube.com"),
        )
    )
    assert cookies == {"SID": "a", "PREF": "b"}


def test_session_header_is_a_standard_cookie_string():
    session = Session({"SID": "a", "HSID": "b"}, "firefox")
    assert session.header() == "SID=a; HSID=b"


# ------------------------------------------------------------------ en-têtes


def test_headers_carry_everything_ytmusicapi_needs():
    headers = build_headers(COOKIE)
    assert headers["cookie"] == COOKIE
    assert headers["x-goog-authuser"] == "0"
    assert headers["user-agent"]
    # ytmusicapi reconnaît le mode navigateur à cet en-tête, qu'il recalcule
    # ensuite à chaque requête.
    assert headers["authorization"].startswith("SAPISIDHASH ")


def test_a_session_without_the_signing_cookie_is_refused_early():
    """Mieux vaut un message clair ici qu'un échec obscur au premier appel d'API."""
    with pytest.raises(BrowserSessionError, match=REQUIRED_COOKIE):
        build_headers("SID=abc; HSID=def")


# -------------------------------------------------------------------- import


def test_import_writes_a_usable_auth_file(tmp_path):
    path = tmp_path / "browser.json"
    source = import_session(
        path, "firefox", reader_of(FakeCookie(REQUIRED_COOKIE, "signature"), FakeCookie("SID", "a"))
    )

    assert source == "firefox"
    stored = json.loads(path.read_text())
    assert REQUIRED_COOKIE in stored["cookie"]
    assert stored["authorization"].startswith("SAPISIDHASH ")


def test_auth_file_is_not_world_readable(tmp_path):
    path = tmp_path / "browser.json"
    import_session(path, None, reader_of(FakeCookie(REQUIRED_COOKIE, "s")))
    assert path.stat().st_mode & 0o077 == 0


def test_a_disconnected_browser_is_reported_clearly(tmp_path):
    with pytest.raises(BrowserSessionError, match="connecte-toi"):
        import_session(tmp_path / "b.json", "firefox", reader_of(FakeCookie("PREF", "x")))


def test_encrypted_cookies_point_to_the_windows_limitation():
    def failing(_browser):
        raise PermissionError("clé de chiffrement inaccessible")

    with pytest.raises(BrowserSessionError, match="Windows"):
        read_session("chrome", failing)


def test_a_missing_browser_is_not_blamed_on_windows():
    """Deux causes fréquentes, deux gestes différents : ne pas les confondre."""

    def failing(_browser):
        raise Exception("Could not find Firefox profile directory")

    with pytest.raises(BrowserSessionError, match="Aucun profil de firefox") as caught:
        read_session("firefox", failing)
    assert "Windows" not in str(caught.value)


def test_an_unexpected_failure_on_firefox_stays_factual():
    def failing(_browser):
        raise Exception("base verrouillée")

    with pytest.raises(BrowserSessionError, match="base verrouillée") as caught:
        read_session("firefox", failing)
    assert "Windows" not in str(caught.value)


def test_an_unknown_browser_is_rejected():
    # La validation a lieu après l'import de la dépendance optionnelle.
    pytest.importorskip("browser_cookie3")
    from ytmgc.sources.browser_session import _default_reader

    with pytest.raises(BrowserSessionError, match="Navigateur inconnu"):
        _default_reader("navigateur-imaginaire")


# ------------------------------------------------- en-têtes collés à la main

CURL_BASH = """curl 'https://music.youtube.com/youtubei/v1/browse?prettyPrint=false' \\
  -H 'accept: */*' \\
  -H 'content-type: application/json' \\
  -H 'cookie: VISITOR_INFO1_LIVE=abc; SID=xyz; __Secure-3PAPISID=signature' \\
  -H 'x-goog-authuser: 2' \\
  --data-raw '{"context":{}}'"""

CURL_CMD = (
    'curl "https://music.youtube.com/youtubei/v1/browse" ^\n'
    '  -H "accept: */*" ^\n'
    '  -H "cookie: SID=xyz; __Secure-3PAPISID=signature"'
)

RAW_HEADERS = "accept: */*\ncookie: SID=xyz; __Secure-3PAPISID=signature\nx-goog-authuser: 0"


def test_curl_from_bash_is_parsed():
    from ytmgc.sources.browser_session import parse_pasted_headers

    headers = parse_pasted_headers(CURL_BASH)
    assert headers["cookie"].endswith("__Secure-3PAPISID=signature")
    assert headers["x-goog-authuser"] == "2"


def test_curl_from_windows_cmd_is_parsed():
    """Chrome sous Windows produit des « ^ » et des guillemets doubles."""
    from ytmgc.sources.browser_session import parse_pasted_headers

    assert "__Secure-3PAPISID" in parse_pasted_headers(CURL_CMD)["cookie"]


def test_plain_headers_are_still_accepted():
    from ytmgc.sources.browser_session import parse_pasted_headers

    assert parse_pasted_headers(RAW_HEADERS)["cookie"].startswith("SID=")


def test_the_request_line_is_not_mistaken_for_a_header():
    from ytmgc.sources.browser_session import parse_pasted_headers

    raw = "POST /youtubei/v1/browse HTTP/2\ncookie: __Secure-3PAPISID=s"
    headers = parse_pasted_headers(raw)
    assert list(headers) == ["cookie"]


def test_cookie_passed_with_the_b_option_is_found():
    from ytmgc.sources.browser_session import parse_pasted_headers

    raw = "curl 'https://music.youtube.com/' -b 'SID=xyz; __Secure-3PAPISID=signature'"
    assert "__Secure-3PAPISID" in parse_pasted_headers(raw)["cookie"]


def test_paste_writes_the_auth_file(tmp_path):
    from ytmgc.sources.browser_session import auth_file_from_paste

    path = tmp_path / "browser.json"
    auth_file_from_paste(CURL_BASH, path)

    stored = json.loads(path.read_text())
    assert stored["x-goog-authuser"] == "2"
    assert stored["authorization"].startswith("SAPISIDHASH ")


def test_authuser_defaults_when_the_request_has_none(tmp_path):
    """Seul le cookie est indispensable : exiger x-goog-authuser obligeait à
    dénicher une requête d'API précise."""
    from ytmgc.sources.browser_session import auth_file_from_paste

    path = tmp_path / "browser.json"
    auth_file_from_paste(CURL_CMD, path)
    assert json.loads(path.read_text())["x-goog-authuser"] == "0"


def test_a_request_without_cookie_says_what_to_do(tmp_path):
    from ytmgc.sources.browser_session import PasteError, auth_file_from_paste

    with pytest.raises(PasteError, match="autre ligne"):
        auth_file_from_paste("curl 'https://music.youtube.com/' -H 'accept: */*'", tmp_path / "b.json")


def test_a_signed_out_session_is_detected(tmp_path):
    from ytmgc.sources.browser_session import PasteError, auth_file_from_paste

    raw = "curl 'https://music.youtube.com/' -H 'cookie: VISITOR_INFO1_LIVE=abc'"
    with pytest.raises(PasteError, match=REQUIRED_COOKIE):
        auth_file_from_paste(raw, tmp_path / "b.json")


def test_powershell_paste_points_to_the_right_menu_entry(tmp_path):
    from ytmgc.sources.browser_session import PasteError, auth_file_from_paste

    raw = 'Invoke-WebRequest -UseBasicParsing -Uri "https://music.youtube.com/"'
    with pytest.raises(PasteError, match="bash"):
        auth_file_from_paste(raw, tmp_path / "b.json")
