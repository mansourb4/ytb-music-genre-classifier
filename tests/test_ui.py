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


#: Aperçu d'exemple injecté dans la page : les tests d'interface portent sur le
#: rendu, pas sur le calcul, déjà couvert côté Python.
SAMPLE_PREVIEW = """
window.__preview = {
  remote_known: true,
  preview: {
    playlists: [{
      key: "rock/grunge", name: "Rock — Grunge", kind: "style", count: 2,
      change: "création", added: 2, removed: 0,
      description: "[ytmgc] key=rock/grunge",
      genre: "Rock", style: "Grunge",
      genre_text: "Issu du rock'n'roll des années 1950.",
      style_text: "Né à Seattle à la fin des années 1980.",
      note: null,
      image: "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
      tracks: [
        {video_id: "g1", title: "Come As You Are", artist: "Nirvana",
         album: "Nevermind", thumbnail: "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
         genres: ["Rock"], styles: ["Grunge"], year: 1991},
        {video_id: "g2", title: "Lithium", artist: "Nirvana", album: "Nevermind",
         thumbnail: null, genres: ["Rock"], styles: ["Grunge"], year: 1991},
      ],
    }],
    obsolete: [], created: 1, updated: 0, unchanged: 0, assignments: 2,
  },
};
renderPreview(window.__preview);
"""


def render_sample_preview(page):
    page.evaluate(f"() => {{ {SAMPLE_PREVIEW} }}")


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
    assert "cookie de session" in page.locator("#connect-msg").text_content()


def test_sort_modes_are_selectable(page):
    page.click('.mode[data-key="genre"]')
    assert "selected" in (page.locator('.mode[data-key="genre"]').get_attribute("class") or "")
    assert "selected" not in (page.locator('.mode[data-key="detaille"]').get_attribute("class") or "")


def test_the_paste_example_follows_the_selected_browser(page):
    """Régression : l'exemple restait celui de Firefox après un passage sur Chrome,
    donc il décrivait un texte que l'utilisateur n'aurait jamais sous les yeux."""
    page.click('.tab[data-tab="headers"]')
    firefox_example = page.locator("#headers").get_attribute("placeholder")
    assert "\\" in firefox_example  # continuation de ligne bash

    page.click('.subtab[data-sub="chrome"]')
    chrome_example = page.locator("#headers").get_attribute("placeholder")
    assert chrome_example != firefox_example
    assert "^" in chrome_example  # continuation de ligne cmd

    page.click('.subtab[data-sub="firefox"]')
    assert page.locator("#headers").get_attribute("placeholder") == firefox_example


def test_a_powershell_paste_is_refused_with_the_right_advice(page):
    page.click('.tab[data-tab="headers"]')
    page.fill("#headers", 'Invoke-WebRequest -Uri "https://music.youtube.com/"')
    page.click("#connect-btn")
    assert "cURL (bash)" in page.locator("#connect-msg").text_content()


def test_a_paste_without_session_cookie_is_refused(page):
    page.click('.tab[data-tab="headers"]')
    page.fill("#headers", "curl 'https://music.youtube.com/' -H 'accept: */*'")
    page.click("#connect-btn")
    assert "autre ligne" in page.locator("#connect-msg").text_content()


def test_the_progress_bar_is_visible_without_scrolling(page):
    """Régression : le panneau de progression était en bas de page, hors écran,
    donc invisible au moment précis où l'utilisateur attend un signe de vie."""
    page.evaluate("""() => {
        document.getElementById('job-box').classList.remove('hidden');
        document.getElementById('job-msg').textContent = 'Analyse en cours…';
    }""")
    box = page.locator("#job-box")
    assert box.is_visible()

    viewport = page.viewport_size["height"]
    top = box.bounding_box()["y"]
    assert top < viewport, "Le bandeau doit être dans la fenêtre, sans défilement"
    assert page.evaluate("getComputedStyle(document.getElementById('job-box')).position") == "fixed"


def test_analysis_without_a_selected_source_is_refused_client_side(page):
    page.evaluate("""() => {
        document.querySelectorAll('#sources-list input').forEach((i) => { i.checked = false; });
    }""")
    page.click("#analyse-btn")
    assert "étape 2" in page.locator("#analyse-msg").text_content()


def test_the_sort_type_comes_before_the_analysis(page):
    """Le tri conditionne l'aperçu produit par l'analyse : le choisir après
    obligeait à revenir en arrière."""
    headings = page.locator("section h2").all_text_contents()
    order = [h for h in headings if "TRI" in h.upper() or "ANALYSER" in h.upper()]
    assert "tri" in order[0].lower()
    assert "analyser" in order[1].lower()


def test_the_preview_lives_in_the_analysis_section(page):
    """L'aperçu n'a plus de section propre : il conclut l'analyse."""
    section = page.locator("section", has=page.locator("#analyse-btn"))
    assert section.locator("#preview-list").count() == 1
    assert section.locator("#preview-summary").count() == 1


def test_preview_rows_expand_to_show_track_details(page):
    """Le détail n'est construit qu'à l'ouverture : replié, il représenterait
    des milliers de lignes inutiles."""
    render_sample_preview(page)

    row = page.locator("details.pl").first
    assert row.locator("img.pl-cover").count() == 1
    assert page.locator("li.track").count() == 0, "Les titres ne doivent pas être construits repliés"

    row.click()
    page.wait_for_selector("li.track")
    assert page.locator("li.track").count() == 2
    facts = page.locator("li.track .track-facts").first.text_content()
    for expected in ["Nevermind", "1991", "Rock", "Grunge"]:
        assert expected in facts
    # La description est présentée en texte mis en forme, non plus en bloc brut.
    description = row.locator(".pl-desc").text_content()
    assert "Genre — Rock" in description and "rock'n'roll" in description
    assert "Style — Grunge" in description and "Seattle" in description
    assert row.locator("pre").count() == 0


def test_a_track_without_artwork_keeps_its_row_aligned(page):
    render_sample_preview(page)
    page.locator("details.pl").first.click()
    page.wait_for_selector("li.track")
    assert page.locator("li.track .cover.empty").count() == 1


def test_playlists_and_tracks_can_be_unchecked(page):
    """La sélection de l'aperçu commande ce qui sera réellement appliqué."""
    render_sample_preview(page)
    row = page.locator("details.pl").first
    row.click()
    page.wait_for_selector("li.track")

    assert page.evaluate("currentExclusions()") == {
        "excluded_playlists": [], "excluded_tracks": {}
    }

    page.locator("li.track .track-check").first.uncheck()
    assert page.evaluate("currentExclusions()") == {
        "excluded_playlists": [], "excluded_tracks": {"rock/grunge": ["g1"]}
    }


def test_unchecking_a_playlist_unchecks_its_tracks(page):
    render_sample_preview(page)
    row = page.locator("details.pl").first
    row.click()
    page.wait_for_selector("li.track")

    page.locator(".pl-check").first.uncheck()

    exclusions = page.evaluate("currentExclusions()")
    assert exclusions["excluded_playlists"] == ["rock/grunge"]
    assert sorted(exclusions["excluded_tracks"]["rock/grunge"]) == ["g1", "g2"]
    assert page.locator("#apply-btn").is_disabled()


def test_the_selection_total_is_shown(page):
    render_sample_preview(page)
    page.locator("details.pl").first.click()
    page.wait_for_selector("li.track")
    assert "2" in page.locator("#preview-selection").text_content()

    page.locator("li.track .track-check").first.uncheck()
    assert "1" in page.locator("#preview-selection").text_content()


def test_the_selected_source_total_is_shown(page):
    total = page.locator("#sources-total")
    assert "source" in total.text_content()

    page.click("#sources-none")
    assert "0" in total.text_content()
