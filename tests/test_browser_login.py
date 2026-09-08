"""Fenêtre de connexion : capture de la session une fois l'utilisateur connecté."""

import json

import pytest

from ytmgc.sources.browser_login import capture_session
from ytmgc.sources.browser_session import REQUIRED_COOKIE, BrowserSessionError


class FakeWindow:
    """Fenêtre dont la connexion aboutit après `after` sondages."""

    def __init__(self, after=2, never=False):
        self.after, self.never, self.polls, self.closed = after, never, 0, False

    def cookies(self):
        self.polls += 1
        if self.never or self.polls <= self.after:
            return [{"name": "PREF", "value": "x"}]
        return [{"name": "PREF", "value": "x"}, {"name": REQUIRED_COOKIE, "value": "signature"}]

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds


def run(path, window, timeout_s=60, **kwargs):
    clock = Clock()
    return capture_session(
        path, lambda: window, timeout_s=timeout_s,
        clock=lambda: clock.now, sleep=clock.sleep, **kwargs
    )


def test_session_is_captured_once_the_user_signs_in(tmp_path):
    path = tmp_path / "browser.json"
    window = FakeWindow(after=2)

    assert run(path, window) == "fenêtre de connexion"
    assert window.polls == 3
    assert json.loads(path.read_text())["cookie"].count(REQUIRED_COOKIE) == 1


def test_the_window_is_always_closed(tmp_path):
    window = FakeWindow(after=1)
    run(tmp_path / "b.json", window)
    assert window.closed is True


def test_timeout_explains_the_google_block_and_closes_the_window(tmp_path):
    window = FakeWindow(never=True)
    with pytest.raises(BrowserSessionError, match="OAuth"):
        run(tmp_path / "b.json", window, timeout_s=10)
    assert window.closed is True


def test_nothing_is_written_when_the_user_never_signs_in(tmp_path):
    path = tmp_path / "browser.json"
    with pytest.raises(BrowserSessionError):
        run(path, FakeWindow(never=True), timeout_s=10)
    assert not path.exists()


def test_progress_is_reported_while_waiting(tmp_path):
    remaining = []
    run(tmp_path / "b.json", FakeWindow(after=2), on_wait=remaining.append)
    assert remaining == [60, 58]  # décompte du temps restant


def test_a_window_that_fails_to_open_is_not_swallowed(tmp_path):
    def failing():
        raise BrowserSessionError("Chromium introuvable")

    with pytest.raises(BrowserSessionError, match="Chromium"):
        capture_session(tmp_path / "b.json", failing)
