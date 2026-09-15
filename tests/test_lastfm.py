"""Tags par titre : la seule source qui distingue deux morceaux d'un album."""

import pytest
from conftest import make_track

from ytmgc.config import LastfmConfig
from ytmgc.sources.lastfm import LastfmClient, LastfmError, parse_tags, query_key


class Response:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload


class Session:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, url, params, headers, timeout):
        self.requests.append(params)
        return self._responses.pop(0)


def client(session):
    return LastfmClient(
        LastfmConfig(api_key="k", rate_limit_per_minute=6000), session=session, sleep=lambda s: None
    )


# ------------------------------------------------------------------ parsing


def test_tags_are_returned_with_their_weight():
    payload = {"toptags": {"tag": [{"name": "mellow", "count": 100}, {"name": "acoustic", "count": 42}]}}
    assert parse_tags(payload) == [("mellow", 100), ("acoustic", 42)]


def test_a_single_tag_comes_back_as_an_object():
    """Quirk de l'API : un seul résultat n'est pas renvoyé dans une liste."""
    assert parse_tags({"toptags": {"tag": {"name": "seul", "count": 5}}}) == [("seul", 5)]


def test_a_weight_given_as_text_is_accepted():
    assert parse_tags({"toptags": {"tag": [{"name": "x", "count": "7"}]}}) == [("x", 7)]


def test_a_malformed_weight_does_not_break_the_run():
    assert parse_tags({"toptags": {"tag": [{"name": "x", "count": "beaucoup"}]}}) == [("x", 0)]


def test_an_unknown_track_yields_no_tag_rather_than_an_error():
    """L'absence de tags est un résultat, pas une panne."""
    assert parse_tags({"error": 6, "message": "Track not found"}) == []
    assert parse_tags({}) == []
    assert parse_tags({"toptags": {}}) == []


def test_a_nameless_tag_is_ignored():
    assert parse_tags({"toptags": {"tag": [{"count": 9}, {"name": "ok", "count": 1}]}}) == [("ok", 1)]


# -------------------------------------------------------------------- cache


def test_the_key_distinguishes_two_tracks_of_one_album():
    """Tout l'intérêt de cette source : Discogs, lui, les confond."""
    lithium = make_track("v1", "Lithium", "Nirvana", album="Nevermind")
    something = make_track("v2", "Something In The Way", "Nirvana", album="Nevermind")
    assert query_key(lithium) != query_key(something)


def test_the_key_ignores_editorial_noise():
    a = make_track("v1", "Lithium (Official Video)", "Nirvana")
    b = make_track("v2", "Lithium", "Nirvana")
    assert query_key(a) == query_key(b)


# ------------------------------------------------------------------- client


def test_the_request_asks_for_the_track_not_the_album():
    session = Session([Response(payload={"toptags": {"tag": []}})])
    client(session).top_tags(make_track("v1", "Lithium", "Nirvana", album="Nevermind"))

    params = session.requests[0]
    assert params["method"] == "track.getTopTags"
    assert params["track"] == "Lithium" and params["artist"] == "Nirvana"
    assert "album" not in params
    assert params["autocorrect"] == 1


def test_a_missing_key_is_caught_at_construction():
    with pytest.raises(LastfmError, match="LASTFM_API_KEY"):
        LastfmClient(LastfmConfig(api_key=""))


def test_a_rejected_key_is_reported_immediately():
    with pytest.raises(LastfmError, match="403"):
        client(Session([Response(403)])).top_tags(make_track("v1", "T", "A"))


def test_rate_limiting_is_retried():
    session = Session([Response(429, headers={"Retry-After": "0"}),
                       Response(payload={"toptags": {"tag": [{"name": "ok", "count": 1}]}})])
    assert client(session).top_tags(make_track("v1", "T", "A")) == [("ok", 1)]


def test_server_errors_are_retried_then_reported():
    with pytest.raises(LastfmError, match="3 tentatives"):
        client(Session([Response(500)] * 3)).top_tags(make_track("v1", "T", "A"))


# ------------------------------------------------- commande de diagnostic


def run_tags_command(monkeypatch, capsys, tags, tmp_path):
    from ytmgc import cli
    from ytmgc.sources import lastfm

    class Fake:
        def __init__(self, *_args, **_kwargs):
            pass

        def top_tags(self, _track):
            return tags

    monkeypatch.setattr(lastfm, "LastfmClient", Fake)
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("LASTFM_API_KEY", "k")
    code = cli.main(["-c", str(config), "tags", "Sex Pistols", "La ballade"])
    return code, capsys.readouterr().out


def test_the_command_explains_what_the_tags_produce(monkeypatch, capsys, tmp_path):
    code, out = run_tags_command(
        monkeypatch, capsys, [("ballad", 90), ("acoustic", 70), ("seen live", 100)], tmp_path
    )

    assert code == 0
    assert "ballad" in out and "90" in out
    assert "Ballad" in out                      # le style qu'il désigne
    assert "Mélancolique" in out                # l'ambiance qui en découle
    assert "seen live" in out                   # listé, mais sans effet


def test_a_weakly_posed_tag_is_shown_as_discarded(monkeypatch, capsys, tmp_path):
    _, out = run_tags_command(monkeypatch, capsys, [("ballad", 2)], tmp_path)
    assert "écarté" in out
    assert "les styles de l'album feront foi" in out


def test_an_unknown_track_says_so_plainly(monkeypatch, capsys, tmp_path):
    code, out = run_tags_command(monkeypatch, capsys, [], tmp_path)
    assert code == 0
    assert "Aucun tag" in out
    assert "inconnu de Last.fm" in out
