"""Tests de l'API web, sans réseau ni serveur réel."""

import pytest
from conftest import make_candidate
from fakes import FakeDiscogs, FakePlaylistClient

from ytmgc.models import RemotePlaylist, Track
from ytmgc.web.app import Services, create_app
from ytmgc.web.jobs import JobRunner

fastapi_testclient = pytest.importorskip("fastapi.testclient")

CATALOGUE = {
    "Nirvana": [make_candidate(1, "Nevermind", "Nirvana", ("Rock",), ("Grunge",))],
    "Aphex Twin": [make_candidate(2, "SAW", "Aphex Twin", ("Electronic",), ("Ambient", "IDM"))],
}
LIBRARY = [Track(f"g{i}", f"Titre {i}", ("Nirvana",), "Nevermind") for i in range(5)]
LIBRARY += [Track(f"e{i}", f"Piste {i}", ("Aphex Twin",), "SAW") for i in range(5)]


class ScanningClient(FakePlaylistClient):
    """Client YouTube complet : lecture de bibliothèque et gestion de playlists."""

    def __init__(self, playlists=None, library=LIBRARY):
        super().__init__(playlists)
        self.library = library

    def scan(self, sources):
        return list(self.library)


@pytest.fixture
def client(repository, config, tmp_path):
    config.youtube.auth_file = str(tmp_path / "browser.json")
    youtube = ScanningClient()
    services = Services(
        config=config,
        repository=repository,
        youtube_factory=lambda _config: youtube,
        discogs_factory=lambda _config: FakeDiscogs(CATALOGUE),
        jobs=JobRunner(),
    )
    test_client = fastapi_testclient.TestClient(create_app(services))
    test_client.youtube = youtube
    test_client.repository = repository
    return test_client


def wait(client, response):
    """Attend la fin du traitement lancé, puis renvoie son état final."""
    job_id = response.json()["id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "en cours":
            return job
    raise AssertionError("Le traitement ne se termine pas")


def analyse(client):
    job = wait(client, client.post("/api/analyse"))
    assert job["status"] == "terminé", job["error"]
    return job


# ------------------------------------------------------------------- lecture


def test_sort_modes_are_exposed_with_their_meaning(client):
    payload = client.get("/api/sort-modes").json()
    assert payload["default"] == "detaille"
    assert {mode["key"] for mode in payload["modes"]} >= {"detaille", "genre", "sans-doublon"}
    assert all(mode["summary"] and mode["detail"] for mode in payload["modes"])


def test_status_reports_an_empty_library(client):
    status = client.get("/api/status").json()
    assert status["tracks"] == 0 and status["classified"] == 0
    assert status["connected"] is False


def test_unknown_job_is_a_404(client):
    assert client.get("/api/jobs/inexistant").status_code == 404


# ------------------------------------------------------------------ analyse


def test_analyse_scans_then_classifies(client):
    analyse(client)
    status = client.get("/api/status").json()
    assert status["tracks"] == 10
    assert status["classified"] == 10
    assert status["unclassified"] == 0


def test_a_second_analysis_reuses_the_cache(client):
    analyse(client)
    job = analyse(client)
    assert "0 titre(s) traité(s)" in job["message"]


# ------------------------------------------------------------------- aperçu


def test_preview_lists_playlists_with_descriptions_and_samples(client):
    analyse(client)
    payload = client.post("/api/preview", json={"sort_mode": "detaille"}).json()
    playlists = payload["preview"]["playlists"]

    assert {p["name"] for p in playlists} == {
        "Rock — Grunge", "Electronic — Ambient", "Electronic — IDM"
    }
    grunge = next(p for p in playlists if p["name"] == "Rock — Grunge")
    assert "GENRE — Rock" in grunge["description"]
    assert grunge["sample"] and grunge["change"] == "création"
    assert payload["actions"]


def test_preview_changes_with_the_sort_mode(client):
    analyse(client)
    detailed = client.post("/api/preview", json={"sort_mode": "detaille"}).json()
    by_genre = client.post("/api/preview", json={"sort_mode": "genre"}).json()
    assert len(by_genre["preview"]["playlists"]) < len(detailed["preview"]["playlists"])


def test_preview_writes_nothing(client):
    analyse(client)
    client.post("/api/preview", json={"sort_mode": "detaille"})
    assert [c for c in client.youtube.calls if c != "list"] == []


def test_unknown_sort_mode_is_rejected(client):
    response = client.post("/api/preview", json={"sort_mode": "n-importe-quoi"})
    assert response.status_code == 400
    assert "Type de tri inconnu" in response.json()["detail"]


def test_preview_reports_updates_against_the_existing_state(client):
    analyse(client)
    plan = next(
        p for p in client.post("/api/preview", json={"sort_mode": "detaille"}).json()["preview"]["playlists"]
        if p["key"] == "rock/grunge"
    )
    client.youtube._playlists["PLx"] = RemotePlaylist(
        "PLx", plan["name"], plan["description"], ("g0", "intrus"), {"g0": "s0", "intrus": "s1"}
    )
    refreshed = client.post("/api/preview", json={"sort_mode": "detaille"}).json()
    grunge = next(p for p in refreshed["preview"]["playlists"] if p["key"] == "rock/grunge")
    assert grunge["change"] == "mise à jour"
    assert grunge["added"] == 4 and grunge["removed"] == 1


# -------------------------------------------------------------- application


def test_apply_requires_an_explicit_confirmation(client):
    analyse(client)
    response = client.post("/api/apply", json={"sort_mode": "detaille"})
    assert response.status_code == 400
    assert client.youtube.list_playlists() == []


def test_apply_creates_the_previewed_playlists(client):
    analyse(client)
    previewed = {
        p["name"] for p in client.post("/api/preview", json={"sort_mode": "detaille"}).json()["preview"]["playlists"]
    }
    job = wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))

    assert job["status"] == "terminé", job["error"]
    assert {p.title for p in client.youtube.list_playlists()} == previewed
    assert client.get("/api/status").json()["managed_playlists"] == len(previewed)


def test_apply_uses_the_requested_sort_mode(client):
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "genre", "confirm": True}))
    assert all("—" not in p.title or "Autres styles" in p.title or "Divers" in p.title
               for p in client.youtube.list_playlists())


def test_applying_twice_changes_nothing(client):
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    before = {p.playlist_id: p.video_ids for p in client.youtube.list_playlists()}
    job = wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    assert job["result"]["applied"] == 0
    assert {p.playlist_id: p.video_ids for p in client.youtube.list_playlists()} == before


# --------------------------------------------------------------- annulation


def test_purge_requires_an_explicit_confirmation(client):
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    assert client.post("/api/purge", json={}).status_code == 400
    assert client.youtube.list_playlists() != []


def test_purge_removes_generated_playlists_and_keeps_the_others(client):
    perso = RemotePlaylist("PLperso", "Ma sélection", "faite à la main", ("g0",))
    client.youtube._playlists["PLperso"] = perso
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))

    job = wait(client, client.post("/api/purge", json={"confirm": True}))

    assert job["status"] == "terminé", job["error"]
    assert client.youtube.list_playlists() == [perso]
    assert client.get("/api/status").json()["managed_playlists"] == 0


def test_purge_then_apply_restores_the_playlists(client):
    """L'annulation doit être réversible en relançant l'application."""
    analyse(client)
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    wait(client, client.post("/api/purge", json={"confirm": True}))
    wait(client, client.post("/api/apply", json={"sort_mode": "detaille", "confirm": True}))
    assert len(client.youtube.list_playlists()) == 3


# ------------------------------------------------------------------- divers


def test_only_one_job_runs_at_a_time(client, monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()

    def slow_scan(_sources):
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr(client.youtube, "scan", slow_scan)
    client.post("/api/analyse")
    started.wait(5)
    try:
        assert client.post("/api/analyse").status_code == 409
    finally:
        release.set()


def test_a_failing_job_reports_its_error(client, monkeypatch):
    def failing(_sources):
        raise RuntimeError("session expirée")

    monkeypatch.setattr(client.youtube, "scan", failing)
    job = wait(client, client.post("/api/analyse"))
    assert job["status"] == "échoué"
    assert "session expirée" in job["error"]


def test_the_interface_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Organiser sa bibliothèque YouTube Music" in response.text


# ------------------------------------------------------ connexion OAuth (web)


def test_connect_methods_reports_what_is_configured(client):
    payload = client.get("/api/connect/methods").json()
    assert payload["oauth_client_configured"] is False
    assert payload["connected"] is False


def test_oauth_start_returns_a_link_and_remembers_the_client(client, monkeypatch):
    """Aucun appel réseau : seule la mécanique de l'application est testée."""
    from ytmgc.sources import oauth

    monkeypatch.setattr(
        oauth, "start_device_flow",
        lambda _client, credentials=None: {
            "url": "https://google/device?user_code=ABC", "user_code": "ABC",
            "device_code": "dev", "interval": 5, "expires_in": 1800,
            "verification_url": "https://google/device",
        },
    )

    response = client.post(
        "/api/connect/oauth/start",
        json={"client_id": "123456789.apps.googleusercontent.com", "client_secret": "secret"},
    )
    assert response.status_code == 200
    assert response.json()["user_code"] == "ABC"
    assert client.get("/api/connect/methods").json()["oauth_client_configured"] is True


def test_oauth_start_rejects_incomplete_credentials(client):
    assert client.post("/api/connect/oauth/start", json={"client_id": "x", "client_secret": "y"}).status_code == 422


def test_oauth_poll_without_a_registered_client_is_refused(client):
    response = client.post("/api/connect/oauth/poll", json={"device_code": "dev"})
    assert response.status_code == 400
    assert "identifiant client" in response.json()["detail"].lower()


def test_pasted_headers_missing_the_cookie_are_refused_with_guidance(client):
    response = client.post("/api/connect", json={"headers": "accept: */*\nuser-agent: Mozilla"})
    assert response.status_code == 400
    assert "cookie" in response.json()["detail"]


def test_a_pasted_curl_command_connects_the_account(client):
    """Le geste identique dans tous les navigateurs : « Copier comme cURL »."""
    curl = (
        "curl 'https://music.youtube.com/youtubei/v1/browse' "
        "-H 'accept: */*' "
        "-H 'cookie: SID=xyz; __Secure-3PAPISID=signature'"
    )
    assert client.post("/api/connect", json={"headers": curl}).status_code == 200
    assert client.get("/api/status").json()["connected"] is True
