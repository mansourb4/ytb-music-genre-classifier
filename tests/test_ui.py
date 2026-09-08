"""Tests de l'interface dans un vrai navigateur.

Le câblage du DOM échappe aux tests Python : deux défauts d'onglets sont passés
au travers de la suite avant d'être vus à l'écran. Ces tests chargent la page
réelle et cliquent dessus.

Ils sont ignorés si Playwright ou son navigateur ne sont pas installés : la
suite doit rester exécutable avec les seules dépendances de base.
"""

import os
import socket
import threading
import time
from pathlib import Path

import pytest
from fakes import FakeDiscogs, FakePlaylistClient

from ytmgc.web.app import Services, create_app
from ytmgc.web.jobs import JobRunner

sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="function")
def server(config, repository):
    """Sert l'application sur un port libre, le temps du test."""
    app = create_app(
        Services(
            config=config,
            repository=repository,
            youtube_factory=lambda _c: FakePlaylistClient(),
            discogs_factory=lambda _c: FakeDiscogs({}),
            jobs=JobRunner(),
        )
    )
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("Le serveur de test n'a pas démarré")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


def launch_chromium(playwright):
    """Lance Chromium, en tolérant les installations non standard.

    `playwright install chromium` fournit la variante attendue par défaut ;
    certains environnements ne fournissent qu'un Chromium complet, dont le
    chemin est alors donné par PLAYWRIGHT_CHROMIUM_EXECUTABLE.
    """
    try:
        return playwright.chromium.launch()
    except Exception as first_error:  # noqa: BLE001 - variante par défaut absente
        fallback = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/opt/pw-browsers/chromium")
        if not Path(fallback).exists():
            pytest.skip(f"Navigateur Playwright indisponible : {first_error}")
        try:
            return playwright.chromium.launch(executable_path=fallback)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Navigateur Playwright indisponible : {exc}")


@pytest.fixture
def page(server):
    with sync_api.sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.goto(server, wait_until="networkidle")
        yield page
        browser.close()


PANELS = ["tab-session", "tab-login", "tab-oauth", "tab-headers"]


def visible_panel(page) -> str:
    shown = [panel for panel in PANELS if page.locator(f"#{panel}").is_visible()]
    assert len(shown) == 1, f"Un seul panneau doit être visible, vu : {shown}"
    return shown[0]


@pytest.mark.parametrize("tab", ["session", "login", "oauth", "headers"])
def test_each_tab_shows_its_own_panel(page, tab):
    page.click(f'.tab[data-tab="{tab}"]')
    assert visible_panel(page) == f"tab-{tab}"


def test_switching_browser_subtab_keeps_the_headers_panel_open(page):
    """Régression : les sous-onglets portent aussi la classe .tab, et leur clic
    déclenchait le gestionnaire principal avec un data-tab indéfini — tous les
    panneaux disparaissaient d'un coup."""
    page.click('.tab[data-tab="headers"]')

    page.click('.subtab[data-sub="chrome"]')
    assert visible_panel(page) == "tab-headers"
    assert page.locator("#sub-chrome").is_visible()
    assert not page.locator("#sub-firefox").is_visible()

    page.click('.subtab[data-sub="firefox"]')
    assert visible_panel(page) == "tab-headers"
    assert page.locator("#sub-firefox").is_visible()
    assert not page.locator("#sub-chrome").is_visible()


def test_the_main_tabs_still_work_after_using_a_subtab(page):
    page.click('.tab[data-tab="headers"]')
    page.click('.subtab[data-sub="chrome"]')
    page.click('.tab[data-tab="session"]')
    assert visible_panel(page) == "tab-session"


def test_incomplete_headers_are_refused_before_any_request(page):
    page.click('.tab[data-tab="headers"]')
    page.fill("#headers", "accept: */*")
    page.click("#connect-btn")
    message = page.locator("#connect-msg").text_content()
    assert "cookie" in message and "/youtubei/v1/" in message


def test_sort_modes_are_selectable(page):
    page.click('.mode[data-key="genre"]')
    assert "selected" in (page.locator('.mode[data-key="genre"]').get_attribute("class") or "")
    assert "selected" not in (page.locator('.mode[data-key="detaille"]').get_attribute("class") or "")
