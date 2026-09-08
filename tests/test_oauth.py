"""Connexion OAuth par code d'appareil — la seule praticable depuis un téléphone."""

import json

import pytest

from ytmgc.sources.oauth import (
    OAuthClient,
    complete_device_flow,
    interpret,
    load_client,
    save_client,
    start_device_flow,
)

CLIENT = OAuthClient("123456789.apps.googleusercontent.com", "secret-google")

CODE = {
    "device_code": "AH-1Ng...",
    "user_code": "ABC-DEF-GHI",
    "verification_url": "https://www.google.com/device",
    "interval": 5,
    "expires_in": 1800,
}
TOKEN = {
    "access_token": "ya29.jeton",
    "refresh_token": "1//rafraichissement",
    "scope": "https://www.googleapis.com/auth/youtube",
    "token_type": "Bearer",
    "expires_in": 3599,
}


class FakeCredentials:
    """Double des identifiants ytmusicapi : compte les appels réseau."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.code_calls = 0
        self.token_calls = 0

    def get_code(self):
        self.code_calls += 1
        return CODE

    def token_from_code(self, device_code):
        self.token_calls += 1
        assert device_code == CODE["device_code"]
        return self._responses.pop(0)


# ------------------------------------------------------------- interprétation


@pytest.mark.parametrize("error", ["authorization_pending", "slow_down"])
def test_pending_responses_keep_the_flow_open(error):
    state, _ = interpret({"error": error})
    assert state == "en attente"


@pytest.mark.parametrize(
    "error, fragment",
    [
        ("expired_token", "expiré"),
        ("access_denied", "refusée"),
        ("invalid_client", "ID et le secret"),
        ("unauthorized_client", "saisie limitée"),
    ],
)
def test_fatal_responses_are_explained_in_plain_language(error, fragment):
    state, message = interpret({"error": error})
    assert state == "échec" and fragment in message


def test_unknown_error_is_reported_verbatim():
    state, message = interpret({"error": "quota_exceeded"})
    assert state == "échec" and "quota_exceeded" in message


def test_success_requires_both_tokens():
    assert interpret(TOKEN)[0] == "connecté"
    assert interpret({"access_token": "seul"})[0] == "échec"
    assert interpret({})[0] == "échec"


# ---------------------------------------------------------------------- flux


def test_start_returns_a_ready_to_open_link():
    """Le lien porte déjà le code : rien à recopier sur un téléphone."""
    flow = start_device_flow(CLIENT, credentials=FakeCredentials([]))
    assert flow["url"] == "https://www.google.com/device?user_code=ABC-DEF-GHI"
    assert flow["user_code"] == "ABC-DEF-GHI"
    assert flow["interval"] == 5


def test_polling_while_pending_writes_nothing(tmp_path):
    token_path = tmp_path / "oauth.json"
    credentials = FakeCredentials([{"error": "authorization_pending"}])

    state, _ = complete_device_flow(CLIENT, CODE["device_code"], token_path, credentials)

    assert state == "en attente"
    assert not token_path.exists()


def test_successful_validation_stores_a_usable_token(tmp_path):
    token_path = tmp_path / "oauth.json"
    credentials = FakeCredentials([TOKEN])

    state, _ = complete_device_flow(CLIENT, CODE["device_code"], token_path, credentials)

    assert state == "connecté"
    stored = json.loads(token_path.read_text())
    assert stored["refresh_token"] == TOKEN["refresh_token"]
    # expires_at est calculé au stockage : sans lui, ytmusicapi ne sait pas
    # quand rafraîchir le jeton.
    assert stored["expires_at"] > 0


def test_the_device_code_is_only_exchanged_once(tmp_path):
    """Régression : le code est à usage unique, l'échanger deux fois échoue."""
    credentials = FakeCredentials([TOKEN])
    complete_device_flow(CLIENT, CODE["device_code"], tmp_path / "oauth.json", credentials)
    assert credentials.token_calls == 1


def test_stored_token_is_not_world_readable(tmp_path):
    token_path = tmp_path / "oauth.json"
    complete_device_flow(CLIENT, CODE["device_code"], token_path, FakeCredentials([TOKEN]))
    assert token_path.stat().st_mode & 0o077 == 0


# ------------------------------------------------------- identifiant client


def test_client_round_trip(tmp_path):
    path = tmp_path / "oauth_client.json"
    save_client(path, CLIENT)
    assert load_client(path, env={}) == CLIENT


def test_saved_client_is_not_world_readable(tmp_path):
    path = tmp_path / "oauth_client.json"
    save_client(path, CLIENT)
    assert path.stat().st_mode & 0o077 == 0


def test_environment_takes_precedence_over_the_file(tmp_path):
    path = tmp_path / "oauth_client.json"
    save_client(path, CLIENT)
    from_env = load_client(
        path, env={"YTMGC_OAUTH_CLIENT_ID": "autre", "YTMGC_OAUTH_CLIENT_SECRET": "chut"}
    )
    assert from_env == OAuthClient("autre", "chut")


def test_missing_or_incomplete_client_yields_none(tmp_path):
    assert load_client(tmp_path / "absent.json", env={}) is None
    partial = tmp_path / "partiel.json"
    partial.write_text(json.dumps({"client_id": "seul"}), encoding="utf-8")
    assert load_client(partial, env={}) is None


def test_incomplete_environment_falls_back_to_the_file(tmp_path):
    path = tmp_path / "oauth_client.json"
    save_client(path, CLIENT)
    assert load_client(path, env={"YTMGC_OAUTH_CLIENT_ID": "sans-secret"}) == CLIENT
