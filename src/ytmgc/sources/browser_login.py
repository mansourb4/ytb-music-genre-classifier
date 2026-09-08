"""Connexion par fenêtre de navigateur.

L'application ouvre une vraie fenêtre sur YouTube Music, l'utilisateur s'y
connecte comme d'habitude, et l'application relève la session une fois la
connexion établie. C'est le parcours le plus proche d'un « connecter mon
compte » ordinaire.

Réserve connue : Google refuse parfois l'authentification dans un navigateur
piloté automatiquement (« this browser or app may not be secure »). Le profil
est donc persistant — une session déjà validée est réutilisée sans nouvelle
connexion — et l'échec, s'il survient, doit renvoyer l'utilisateur vers une
autre méthode plutôt que de le laisser attendre.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Protocol

from ytmgc.sources.browser_session import REQUIRED_COOKIE, BrowserSessionError, write_auth_file

MUSIC_URL = "https://music.youtube.com"
#: Au-delà, on considère que l'utilisateur a renoncé ou que Google a bloqué.
DEFAULT_TIMEOUT_S = 300


class BrowserWindow(Protocol):
    """Fenêtre pilotée, réduite à ce dont la capture a besoin."""

    def cookies(self) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


def capture_session(
    path: str | Path,
    window_factory: Callable[[], BrowserWindow],
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    poll_s: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_wait: Callable[[int], None] | None = None,
) -> str:
    """Attend que l'utilisateur se connecte, puis écrit le fichier d'authentification.

    La fenêtre est toujours refermée, y compris en cas d'expiration : un
    navigateur resté ouvert en arrière-plan serait invisible depuis l'interface.
    """
    window = window_factory()
    deadline = clock() + timeout_s
    try:
        while True:
            cookies = {c.get("name"): c.get("value") for c in window.cookies()}
            if cookies.get(REQUIRED_COOKIE):
                header = "; ".join(f"{n}={v}" for n, v in cookies.items() if n)
                write_auth_file(header, path)
                return "fenêtre de connexion"

            remaining = int(deadline - clock())
            if remaining <= 0:
                raise BrowserSessionError(
                    "Aucune connexion détectée dans le temps imparti. "
                    "Si Google a refusé la connexion dans cette fenêtre, utilise "
                    "la reprise de session du navigateur ou la méthode OAuth."
                )
            if on_wait is not None:
                on_wait(remaining)
            sleep(poll_s)
    finally:
        try:
            window.close()
        except Exception:  # noqa: BLE001 - la fermeture ne doit jamais masquer l'erreur utile
            pass


class _PlaywrightWindow:
    """Fenêtre Chromium persistante, ouverte sur YouTube Music."""

    def __init__(self, profile_dir: str | Path) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - dépendance optionnelle
            raise BrowserSessionError(
                "Cette méthode requiert une dépendance supplémentaire :\n"
                '  pip install "ytmgc[browser]" && playwright install chromium'
            ) from exc

        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        # Profil persistant : une connexion déjà validée n'est pas redemandée,
        # ce qui limite les refus de Google sur les navigateurs pilotés.
        self._context = self._playwright.chromium.launch_persistent_context(
            str(profile_dir), headless=False, args=["--disable-blink-features=AutomationControlled"]
        )
        page = self._context.pages[0] if self._context.pages else self._context.new_page()
        page.goto(MUSIC_URL)

    def cookies(self) -> list[dict[str, Any]]:
        return self._context.cookies()

    def close(self) -> None:
        self._context.close()
        self._playwright.stop()


def login_and_capture(
    path: str | Path,
    profile_dir: str | Path = ".browser-profile",
    **kwargs: Any,
) -> str:
    """Ouvre la fenêtre de connexion et capture la session obtenue."""
    return capture_session(path, lambda: _PlaywrightWindow(profile_dir), **kwargs)
