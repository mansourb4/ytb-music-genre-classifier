import pytest
from conftest import make_track

from ytmgc.config import DiscogsConfig
from ytmgc.sources.discogs import DiscogsClient, DiscogsError, parse_results, query_key


class Response:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {"results": []}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class Session:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, url, params, headers, timeout):
        self.requests.append(params)
        return self._responses.pop(0)


def client(session, rate=600):
    return DiscogsClient(
        DiscogsConfig(token="t", rate_limit_per_minute=rate), session=session, sleep=lambda s: None
    )


def test_parse_results_splits_artist_from_release_title():
    payload = {"results": [
        {"id": 7, "title": "Aphex Twin - Windowlicker", "year": "1999",
         "genre": ["Electronic"], "style": ["IDM", "Breakbeat"], "type": "release"}
    ]}
    candidate = parse_results(payload, 10)[0]
    assert (candidate.artist, candidate.title, candidate.year) == ("Aphex Twin", "Windowlicker", 1999)
    assert candidate.styles == ("IDM", "Breakbeat")


def test_parse_results_tolerates_missing_and_malformed_fields():
    payload = {"results": [{"id": 1, "title": "Sans année", "year": "inconnu"}]}
    candidate = parse_results(payload, 10)[0]
    assert candidate.year is None and candidate.genres == () and candidate.styles == ()


def test_query_key_groups_tracks_of_one_album():
    a = make_track("v1", "Lithium", "Nirvana", album="Nevermind")
    b = make_track("v2", "In Bloom", "Nirvana", album="Nevermind (Remastered)")
    assert query_key(a) == query_key(b) == "nirvana::nevermind"


def test_search_prefers_the_album_because_discogs_tags_releases():
    session = Session([Response()])
    client(session).search(make_track("v1", "Lithium", "Nirvana", album="Nevermind"))
    assert session.requests[0]["release_title"] == "Nevermind"
    assert "track" not in session.requests[0]


def test_search_falls_back_to_the_track_without_an_album():
    session = Session([Response()])
    client(session).search(make_track("v1", "Windowlicker", "Aphex Twin"))
    assert session.requests[0]["track"] == "Windowlicker"


def test_rate_limit_response_is_retried():
    session = Session([Response(429, headers={"Retry-After": "0"}), Response()])
    client(session).search(make_track("v1", "T", "A"))
    assert len(session.requests) == 2


def test_server_errors_are_retried_then_reported():
    session = Session([Response(500)] * 4)
    with pytest.raises(DiscogsError, match="4 tentatives"):
        client(session).search(make_track("v1", "T", "A"))


def test_bad_token_fails_immediately():
    session = Session([Response(401)])
    with pytest.raises(DiscogsError, match="401"):
        client(session).search(make_track("v1", "T", "A"))
    assert len(session.requests) == 1


def test_missing_token_is_caught_at_construction():
    with pytest.raises(DiscogsError, match="DISCOGS_TOKEN"):
        DiscogsClient(DiscogsConfig(token=""))
