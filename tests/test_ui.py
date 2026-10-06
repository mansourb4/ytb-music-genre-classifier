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


def test_the_steps_follow_the_real_order_of_the_work(page):
    """Chaque étape ne dépend que des précédentes : analyser, affiner, puis
    choisir le tri, prévisualiser et appliquer. L'ancien ordre plaçait le tri
    avant l'analyse, et l'aperçu avant les verdicts qui le modifient."""
    titles = [
        h.split("\n")[0].strip().lower()
        for h in page.locator("section h2").all_text_contents()
        if h.strip()[:1].isdigit()
    ]
    expected = ["connecter", "choisir ce qui", "analyser", "affiner", "type de tri",
                "prévisualiser", "appliquer"]
    assert len(titles) == len(expected)
    for title, word in zip(titles, expected):
        assert word in title, (title, word)


def test_the_preview_has_its_own_step_after_the_sort_type(page):
    analyse = page.locator("section", has=page.locator("#analyse-btn"))
    preview = page.locator("section", has=page.locator("#preview-list"))

    assert analyse.locator("#preview-list").count() == 0
    assert preview.locator("#preview-btn").count() == 1
    assert "Prévisualiser" in preview.locator("h2").text_content()


def test_preview_rows_expand_to_show_track_details(page):
    """Le détail n'est construit qu'à l'ouverture : replié, il représenterait
    des milliers de lignes inutiles."""
    render_sample_preview(page)

    row = page.locator("#preview-list details.pl").first
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
    page.locator("#preview-list details.pl").first.click()
    page.wait_for_selector("li.track")
    assert page.locator("li.track .cover.empty").count() == 1


def test_playlists_and_tracks_can_be_unchecked(page):
    """La sélection de l'aperçu commande ce qui sera réellement appliqué."""
    render_sample_preview(page)
    row = page.locator("#preview-list details.pl").first
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
    row = page.locator("#preview-list details.pl").first
    row.click()
    page.wait_for_selector("li.track")

    page.locator(".pl-check").first.uncheck()

    exclusions = page.evaluate("currentExclusions()")
    assert exclusions["excluded_playlists"] == ["rock/grunge"]
    assert sorted(exclusions["excluded_tracks"]["rock/grunge"]) == ["g1", "g2"]
    assert page.locator("#apply-btn").is_disabled()


def test_the_selection_total_is_shown(page):
    render_sample_preview(page)
    page.locator("#preview-list details.pl").first.click()
    page.wait_for_selector("li.track")
    assert "2" in page.locator("#preview-selection").text_content()

    page.locator("li.track .track-check").first.uncheck()
    assert "1" in page.locator("#preview-selection").text_content()


def test_the_selected_source_total_is_shown(page):
    total = page.locator("#sources-total")
    assert "source" in total.text_content()

    page.click("#sources-none")
    assert "0" in total.text_content()


def test_the_purge_shows_what_it_found_before_deleting(page):
    """La suppression est la seule opération irréversible : elle doit d'abord
    montrer sa cible, et n'agir que sur ce qui reste coché."""
    page.route("**/api/purge/candidates", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"marker": "\\u2731", "playlists": ['
             '{"playlist_id": "PL1", "title": "Rock — Grunge", "count": 12,'
             ' "thumbnail": null, "key": "rock/grunge"},'
             '{"playlist_id": "PL2", "title": "Jazz — Modal", "count": 5,'
             ' "thumbnail": null, "key": null}]}',
    ))

    assert page.locator("#purge-actions").is_hidden()
    page.click("#purge-scan")
    page.wait_for_selector(".purge-row")

    assert page.locator(".purge-row").count() == 2
    assert "2 sur 2" in page.locator("#purge-count").text_content()
    # Une playlist que la base ne connaît pas est signalée comme telle.
    assert page.locator(".purge-row .orphan").count() == 1

    page.locator(".purge-row input").first.uncheck()
    assert "1 sur 2" in page.locator("#purge-count").text_content()
    assert page.evaluate("purgeSelection()") == ["PL2"]


def test_nothing_selected_disables_the_deletion(page):
    page.route("**/api/purge/candidates", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"marker": "\\u2731", "playlists": [{"playlist_id": "PL1", "title": "Rock",'
             ' "count": 3, "thumbnail": null, "key": "rock"}]}',
    ))
    page.click("#purge-scan")
    page.wait_for_selector(".purge-row")

    page.click("#purge-none")
    assert page.locator("#purge-btn").is_disabled()


def test_an_account_without_generated_playlists_says_so(page):
    page.route("**/api/purge/candidates", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"marker": "\\u2731", "playlists": []}',
    ))
    page.click("#purge-scan")
    page.wait_for_function("document.getElementById('purge-msg').textContent.includes('Aucune')")
    assert page.locator("#purge-actions").is_hidden()


def test_source_counts_are_measured_and_summed(page):
    """Régression : le décompte annoncé par ytmusicapi valait « 2 » pour une
    playlist de 2 188 titres, et un mot pour les playlists automatiques —
    donnant NaN une fois converti."""
    counts = {"library": 25, "liked": 312, "uploads": 0, "playlist:PL1": 2188}
    page.route("**/api/sources", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"reachable": true, "defaults": ["library", "liked"],'
             ' "special": [{"key": "library", "label": "Bibliothèque"},'
             '             {"key": "liked", "label": "Titres likés"},'
             '             {"key": "uploads", "label": "Mises en ligne"}],'
             ' "playlists": [{"key": "playlist:PL1", "label": "Favorite Songs", "thumbnail": null}]}',
    ))
    page.route("**/api/sources/count**", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"source": "x", "count": %d}' % counts[
            route.request.url.split("source=")[1].replace("%3A", ":")
        ],
    ))
    page.evaluate("() => { sourceCounts.clear(); }")
    page.evaluate("loadSources()")

    page.wait_for_function(
        "!document.getElementById('sources-total').textContent.includes('en cours')", timeout=15000
    )
    total = page.locator("#sources-total").text_content()
    assert "337" in total  # 25 + 312, les deux sources cochées par défaut
    assert "NaN" not in total

    page.check('#sources-list input[value="playlist:PL1"]')
    assert "2525" in page.locator("#sources-total").text_content()
    assert "2188 titres" in page.locator("#sources-list").text_content()


PREVIEW_BODY = (
    '{"remote_known": true, "actions": [], "preview": {"playlists": [], "obsolete": [],'
    ' "created": 0, "updated": 0, "unchanged": 0, "assignments": 0}}'
)

#: Espionne les appels à l'indicateur sans en changer le comportement. Retarder
#: une réponse depuis un gestionnaire de route bloquerait le fil de Playwright
#: lui-même, et tout se résoudrait d'un bloc.
SPY_ACTIVITY = """
window.__activity = [];
const __begin = beginActivity;
beginActivity = (title, message) => {
  window.__activity.push(title);
  return __begin(title, message);
};
"""


def test_the_activity_bar_shows_then_hides(page):
    taken = page.evaluate("() => beginActivity('Aperçu', 'Calcul en cours…')")
    assert taken is True
    assert page.locator("#job-box").is_visible()
    assert "Aperçu" in page.locator("#job-kind").text_content()
    # Sans total connu, une barre défilante plutôt qu'une jauge figée.
    assert "indeterminate" in (page.locator("#job-bar").get_attribute("class") or "")

    page.evaluate("() => endActivity(true)")
    assert page.locator("#job-box").is_hidden()


def test_the_preview_announces_itself_while_it_computes(page):
    page.route("**/api/preview", lambda route: route.fulfill(
        status=200, content_type="application/json", body=PREVIEW_BODY))
    page.evaluate(f"() => {{ {SPY_ACTIVITY} }}")

    page.evaluate("() => refreshPreview()")

    assert page.evaluate("window.__activity") == ["Aperçu"]
    assert page.locator("#job-box").is_hidden(), "Le bandeau doit disparaître une fois calculé"


def test_the_purge_search_announces_itself(page):
    page.route("**/api/purge/candidates", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"marker": "\u2731", "playlists": []}'))
    page.evaluate(f"() => {{ {SPY_ACTIVITY} }}")

    page.click("#purge-scan")
    page.wait_for_function("window.__activity.length > 0", timeout=10000)

    assert page.evaluate("window.__activity") == ["Recherche"]
    page.wait_for_selector("#job-box", state="hidden", timeout=10000)


def test_a_running_job_keeps_the_bar_for_itself(page):
    """L'avancement chiffré d'un traitement prime sur un simple indicateur."""
    page.evaluate("() => { pollTimer = setInterval(() => {}, 10000); }")
    try:
        assert page.evaluate("beginActivity('Aperçu', 'test')") is False
        assert page.locator("#job-box").is_hidden()
    finally:
        page.evaluate("() => { clearInterval(pollTimer); pollTimer = null; }")


def test_track_tags_are_shown_to_explain_the_placement(page):
    """Un titre classé autrement que son album doit pouvoir s'expliquer."""
    page.evaluate("""() => {
      renderPreview({
        remote_known: true,
        preview: {
          playlists: [{
            key: "ambiance/calme", name: "Ambiance — Calme", kind: "style", count: 1,
            change: "création", added: 1, removed: 0, description: "", axis: "mood",
            genre: "Ambiance", style: "Calme", style_text: "Musiques posées.",
            gathered: ["Ambient"], note: null, image: null,
            tracks: [{video_id: "v2", title: "La ballade", artist: "Sex Pistols",
                      album: "Bollocks", thumbnail: null, genres: ["Rock"],
                      styles: ["Ballad"], year: 1977, tags: ["ballad", "acoustic", "mellow"]}],
          }],
          obsolete: [], created: 1, updated: 0, unchanged: 0, assignments: 1,
        },
      });
    }""")
    page.locator("#preview-list details.pl").first.click()
    page.wait_for_selector("li.track")

    tags = page.locator(".track-tags").text_content()
    assert "ballad" in tags and "acoustic" in tags
    assert "Ballad" in page.locator(".track-facts").text_content()


#: Une playlist du tri « Par famille » : règle, taille hors cible, verdict peu sûr.
FAMILY_PREVIEW = """() => {
  renderPreview({
    remote_known: true,
    preview: {
      playlists: [{
        key: "plan/jazz-fusion", name: "Jazz · Fusion", kind: "plan", count: 2,
        change: "création", added: 2, removed: 0, description: "", axis: "plan",
        note: "Jazz électrique et jazz-rock.", image: null,
        criteria: ["style Fusion", "genre Jazz ; ambiance Cérébral"],
        size_warning: "grande (170 titres, au-delà de 150) : à découper", unsure: 1,
        tracks: [
          {video_id: "f1", title: "Birdland", artist: "Weather Report", album: null,
           thumbnail: null, genres: ["Jazz"], styles: ["Fusion"], year: null, mood: "Groovy",
           note: "Hymne fusion.", confidence: 0.9, unsure: false},
          {video_id: "f2", title: "Babel Live", artist: "Cos", album: null,
           thumbnail: null, genres: ["Jazz"], styles: ["Fusion"], year: null, mood: "Cérébral",
           note: "Jazz-rock complexe en live.", confidence: 0.3, unsure: true},
        ],
      }],
      obsolete: [], created: 1, updated: 0, unchanged: 0, assignments: 2, total_tracks: 3,
      unsorted: [{video_id: "x1", label: "Quelqu'un – Skit", thumbnail: null,
                  reason: "no_rule", detail: "Hors-musique / Skit · Calme"}],
      unsorted_by_reason: {no_rule: 1},
      reason_labels: {no_rule: "aucune playlist du plan ne l'accepte"},
    },
  });
}"""


def test_a_family_playlist_shows_its_rules_and_size(page):
    page.evaluate(FAMILY_PREVIEW)
    summary = page.locator("#preview-list details.pl summary").text_content()
    assert "taille" in summary

    page.locator("#preview-list details.pl").click()
    body = page.locator("#preview-list details.pl .pl-desc").text_content()
    assert "Jazz électrique et jazz-rock." in body
    assert "l'une ou l'autre" in body and "genre Jazz ; ambiance Cérébral" in body
    assert "à découper" in body
    assert "1 titre(s) au verdict peu sûr" in body


def test_an_unsure_verdict_is_marked_and_explained(page):
    page.evaluate(FAMILY_PREVIEW)
    page.locator("#preview-list details.pl").click()
    page.wait_for_selector("#preview-list li.track")
    rows = page.locator("#preview-list li.track")

    assert "à vérifier" not in rows.nth(0).text_content()
    assert "à vérifier" in rows.nth(1).text_content()
    assert "Jazz-rock complexe en live." in rows.nth(1).text_content()
    assert "Cérébral" in rows.nth(1).locator(".track-facts").text_content()


def test_a_track_no_rule_accepts_shows_what_it_is(page):
    """De quoi écrire la règle qui manque, sans aller lire verdicts.txt."""
    page.evaluate(FAMILY_PREVIEW)
    page.locator("#unsorted details.pl").click()
    text = page.locator("#unsorted").text_content()
    assert "aucune playlist du plan ne l'accepte" in text
    assert "Hors-musique / Skit · Calme" in text


def test_unsorted_tracks_do_not_break_the_selection_total(page):
    """Régression : avec des playlists ET des non-rangés, le décompte lisait la
    case à cocher d'une section qui n'en a pas. L'aperçu s'interrompait, le
    total manquait et le bouton Appliquer restait désactivé."""
    page.evaluate(FAMILY_PREVIEW)

    assert "1</b> playlist(s) sur 1" in page.locator("#preview-selection").inner_html()
    assert page.locator("#apply-btn").is_enabled()


def test_the_family_sort_is_offered_first(page):
    page.wait_for_selector(".mode")
    assert page.locator(".mode").first.get_attribute("data-key") == "familles"


#: Aperçu comportant des titres écartés, avec deux causes distinctes.
UNSORTED_PREVIEW = """() => {
  renderPreview({
    remote_known: true,
    preview: {
      playlists: [],
      obsolete: [], created: 0, updated: 0, unchanged: 0, assignments: 0,
      total_tracks: 5,
      unsorted: [
        {video_id: "x1", label: "Personne – Inconnu", thumbnail: null, reason: "unmatched"},
        {video_id: "x2", label: "Quelqu'un – Autre", thumbnail: null, reason: "unmatched"},
        {video_id: "x3", label: "Nirvana – Something", thumbnail: null, reason: "review"},
      ],
      unsorted_by_reason: {unmatched: 2, review: 1},
      reason_labels: {unmatched: "aucune correspondance Discogs",
                      review: "appariement trop incertain"},
    },
  });
}"""


def test_unsorted_tracks_are_shown_with_their_cause(page):
    """Régression : les titres écartés disparaissaient sans un mot, laissant
    croire que l'analyse en avait oublié."""
    page.evaluate(UNSORTED_PREVIEW)

    summary = page.locator("#unsorted details.pl summary").text_content()
    assert "3 titre(s) non rangé(s)" in summary
    assert "sur 5" in summary

    page.locator("#unsorted details.pl").click()
    chips = page.locator("#unsorted .reasons").text_content()
    assert "2 aucune correspondance Discogs" in chips
    assert "1 appariement trop incertain" in chips
    assert page.locator("#unsorted .reason-group").count() == 2
    assert "Personne – Inconnu" in page.locator("#unsorted").text_content()


def test_a_fully_sorted_library_shows_no_unsorted_section(page):
    render_sample_preview(page)
    assert page.locator("#unsorted details").count() == 0


def test_a_new_preview_clears_the_previous_unsorted_list(page):
    page.evaluate(UNSORTED_PREVIEW)
    assert page.locator("#unsorted details").count() == 1
    render_sample_preview(page)
    assert page.locator("#unsorted details").count() == 0


#: État de la passe modèle, tel que /api/enrich/state le renvoie.
JUDGE_STATE = """() => {
  renderJudgeState({
    available: true, model: "claude-opus-5",
    verdicts: 12, verdicts_file: "data/verdicts.txt",
    pending_tracks: 2500,
    scopes: [
      {key: "pilot", label: "Essai — 50 titres", hint: "À faire en premier.",
       tracks: 50, dollars: 0.12, line: "50 titre(s) à juger… ~0.12 $.",
       request: {limit: 50, only_unsorted: false}},
      {key: "unsorted", label: "Titres non rangés — 300", hint: "Ceux que Discogs ignore.",
       tracks: 300, dollars: 0.71, line: "300 titre(s) à juger… ~0.71 $.",
       request: {limit: 0, only_unsorted: true}},
      {key: "all", label: "Toute la bibliothèque — 2500", hint: "Tous les titres sans verdict.",
       tracks: 2500, dollars: 5.95, line: "2500 titre(s) à juger… ~5.95 $.",
       request: {limit: 0, only_unsorted: false}},
    ],
    batch: null,
  });
}"""


def use_api(page):
    """Passe sur l'onglet de la voie payante, comme le ferait l'utilisateur."""
    page.click('#judge-methods [data-method="api"]')


def test_the_cost_is_shown_before_anything_is_spent(page):
    use_api(page)
    page.evaluate(JUDGE_STATE)

    assert page.locator("#judge-btn").is_enabled()
    assert page.locator(".scope").count() == 3


def test_the_trial_run_is_the_default_choice(page):
    """Se tromper sur toute une bibliothèque coûte cent fois plus que sur
    cinquante titres : c'est l'essai qui doit être proposé d'emblée."""
    use_api(page)
    page.evaluate(JUDGE_STATE)

    assert "selected" in page.locator(".scope").first.get_attribute("class")
    assert "~0.12 $" in page.locator("#judge-cost").text_content()
    assert "Lancer l'essai" in page.locator("#judge-btn").text_content()


def test_choosing_another_scope_restates_the_price(page):
    """Le montant affiché doit correspondre à ce qui sera réellement envoyé."""
    use_api(page)
    page.evaluate(JUDGE_STATE)
    page.check('.scope input[value="all"]')

    assert "~5.95 $" in page.locator("#judge-cost").text_content()
    assert "Faire juger" in page.locator("#judge-btn").text_content()


def test_every_scope_shows_its_own_price(page):
    use_api(page)
    page.evaluate(JUDGE_STATE)
    prices = page.locator(".scope .price").all_text_contents()

    assert prices == ["~0.12 $", "~0.71 $", "~5.95 $"]


def test_an_empty_scope_cannot_be_chosen(page):
    page.evaluate("""() => {
      renderJudgeState({
        available: true, model: "claude-opus-5", verdicts: 0,
        verdicts_file: "data/verdicts.txt", pending_tracks: 0,
        scopes: [
          {key: "pilot", label: "Essai — 0 titres", hint: "…", tracks: 0, dollars: 0,
           line: "…", request: {limit: 50, only_unsorted: false}},
          {key: "unsorted", label: "Non rangés — 0", hint: "…", tracks: 0, dollars: 0,
           line: "…", request: {limit: 0, only_unsorted: true}},
          {key: "all", label: "Tout — 0", hint: "…", tracks: 0, dollars: 0,
           line: "…", request: {limit: 0, only_unsorted: false}},
        ],
        batch: null,
      });
    }""")

    assert page.locator(".scope input").first.is_disabled()
    assert page.locator("#judge-btn").is_disabled()
    assert "déjà un verdict" in page.locator("#judge-cost").text_content()


def test_nothing_is_sent_before_an_explicit_confirmation(page):
    """Régression de principe : un clic ne doit jamais suffire à dépenser."""
    use_api(page)
    page.evaluate("""() => {
      window.__posts = [];
      const real = window.fetch;
      window.fetch = (url, options) => { window.__posts.push(url); return real(url, options); };
    }""")
    page.evaluate(JUDGE_STATE)
    page.check('.scope input[value="all"]')
    page.click("#judge-btn")

    assert page.locator("#judge-confirm").is_visible()
    assert "5.95" in page.locator("#judge-confirm-text").text_content()
    assert page.evaluate("() => window.__posts.filter((u) => u.includes('/api/enrich'))") == []


def test_cancelling_the_confirmation_sends_nothing(page):
    use_api(page)
    page.evaluate(JUDGE_STATE)
    page.click("#judge-btn")
    page.click("#judge-cancel")

    assert not page.locator("#judge-confirm").is_visible()


def test_a_batch_already_paid_for_is_offered_as_a_resume(page):
    page.evaluate("""() => {
      renderJudgeState({
        available: true, model: "claude-opus-5", verdicts: 0,
        verdicts_file: "data/verdicts.txt", pending_tracks: 100, scopes: [],
        batch: {id: "msgbatch_1", tracks: 100, created_at: "2026-09-15T10:00:00Z"},
      });
    }""")

    assert "Reprendre le lot" in page.locator("#judge-btn").text_content()
    assert "sans être payé une seconde fois" in page.locator("#judge-cost").text_content()


def test_without_a_key_the_section_says_what_to_do(page):
    page.evaluate("""() => {
      renderJudgeState({
        available: false, model: "claude-opus-5", verdicts: 0,
        verdicts_file: "data/verdicts.txt", pending_tracks: 10, scopes: [],
        batch: null,
      });
    }""")

    assert "ANTHROPIC_API_KEY" in page.locator("#judge-cost").text_content()
    assert page.locator("#judge-btn").is_disabled()


def test_a_looked_up_track_shows_its_genre_style_and_mood(page):
    page.evaluate("""() => {
      renderVerdict({cached: false, verdict: {
        artist: "Nirvana", title: "Something In The Way",
        genre: "Rock", style: "Acoustic", mood: "Mélancolique",
        confidence: 0.92, source: "claude",
        note: "Berceuse sépulcrale, voix au bord du souffle.",
        genre_text: "Issu du rock'n'roll des années 1950.",
        style_text: "Guitare acoustique en avant.",
        mood_text: "Le registre de la peine et du souvenir.",
      }});
    }""")
    text = page.locator("#lookup-result").text_content()

    assert "Rock" in text and "Acoustic" in text and "Mélancolique" in text
    assert "92 %" in text
    assert "Berceuse sépulcrale" in text
    assert "Écrit dans le fichier" in page.locator("#lookup-msg").text_content()


def test_a_known_track_says_that_nothing_was_charged(page):
    page.evaluate("""() => {
      renderVerdict({cached: true, verdict: {
        artist: "Nirvana", title: "Lithium", genre: "Rock", style: "Grunge",
        mood: "Énergique", confidence: 0.8, source: "manuel", note: "",
        genre_text: null, style_text: null, mood_text: null,
      }});
    }""")

    assert "rien n'a été facturé" in page.locator("#lookup-msg").text_content()


def test_a_lookup_without_artist_is_refused_before_any_request(page):
    page.fill("#lookup-title", "Lithium")
    page.click("#lookup-btn")

    assert "l'artiste et le titre" in page.locator("#lookup-msg").text_content()



# ------------------------------------------------ passe gratuite (Claude.ai)


def test_without_an_api_key_the_free_route_is_shown_first(page):
    """Sans clé, proposer d'abord la voie payante, c'est proposer une impasse."""
    assert page.locator("#method-chat").is_visible()
    assert not page.locator("#method-api").is_visible()


def test_the_two_routes_can_be_switched(page):
    use_api(page)
    assert page.locator("#method-api").is_visible()
    assert not page.locator("#method-chat").is_visible()

    page.click('#judge-methods [data-method="chat"]')
    assert page.locator("#method-chat").is_visible()


def test_the_method_tabs_do_not_disturb_the_connection_tabs(page):
    """Régression connue : des boutons portant la classe .tab avaient déjà
    fait disparaître tous les panneaux de connexion."""
    page.click('#judge-methods [data-method="api"]')
    assert visible_panel(page) in PANELS


def test_an_empty_library_has_nothing_to_export(page):
    page.click("#chat-export")
    page.wait_for_function(
        "() => document.querySelector('#chat-export-msg').textContent.includes('Rien')"
    )
    assert page.locator(".chat-file").count() == 0


def test_importing_nothing_is_refused_before_any_request(page):
    page.click("#chat-import")
    assert "Colle d'abord" in page.locator("#chat-msg").text_content()


def test_results_say_where_they_live_even_before_any_verdict(page, config):
    """Régression de clarté : on importait sans savoir où allait le résultat."""
    from pathlib import Path

    page.wait_for_function("() => document.querySelector('#results-file').textContent.includes('/')")
    assert page.locator("#results-file").text_content() == str(
        Path(config.claude.verdicts_file).resolve())


def test_the_free_route_end_to_end(page, repository, config):
    """Exporter, recoller une réponse, et voir ce qui est fait, ce qui reste
    et à quoi ressemble le résultat : le parcours réel, contre le vrai serveur."""
    from pathlib import Path

    from ytmgc.models import Track

    repository.upsert_tracks([
        Track("g1", "Something In The Way", ("Nirvana",), "Nevermind"),
        Track("g2", "Get Lucky", ("Daft Punk", "Pharrell Williams"), "RAM"),
    ])

    # 1. Export : un fichier, téléchargeable, avec son avancement.
    page.click("#chat-export")
    page.wait_for_selector(".chat-file")
    assert "titres-1-sur-1.txt" in page.locator(".chat-file").text_content()
    assert "0 / 2 jugés" in page.locator(".chat-file").text_content()

    # 2. Import d'une réponse partielle.
    page.fill("#chat-answer",
              "```\nNirvana | Something In The Way | Rock | Acoustic | Mélancolique | 0.95 | "
              "Berceuse sépulcrale.\n```")
    page.click("#chat-import")
    page.wait_for_function(
        "() => document.querySelector('#chat-msg').textContent.includes('enregistré')"
    )
    assert "1 morceaux jugés sur 2" in page.locator("#chat-msg").text_content()
    assert "1 / 2 jugés" in page.locator(".chat-file").text_content()
    assert page.locator("#chat-answer").input_value() == ""
    # Régression : le chemin du dossier disparaissait après un import.
    assert str(Path(config.claude.export_dir).resolve()) in page.locator("#chat-export-msg").text_content()

    # 3. Ce qui est fait, ce qui reste, à quoi ressemble le résultat.
    page.wait_for_function(
        "() => document.querySelector('#results-count').textContent.includes('1 morceaux jugés sur 2')"
    )
    page.click("#results-details summary")
    page.click('#results-filter [data-show="done"]')
    page.wait_for_function(
        "() => document.querySelectorAll('#results-rows tr').length === 1"
    )
    done = page.locator("#results-rows").text_content()
    assert "Something In The Way" in done and "Mélancolique" in done
    assert "Berceuse sépulcrale" in done

    page.click('#results-filter [data-show="todo"]')
    page.wait_for_function(
        "() => document.querySelector('#results-rows').textContent.includes('Get Lucky')"
    )
    assert "Something In The Way" not in page.locator("#results-rows").text_content()


def test_results_can_be_searched(page, repository, config):
    from ytmgc.models import Track

    repository.upsert_tracks([
        Track("g1", "Something In The Way", ("Nirvana",), "Nevermind"),
        Track("g2", "Get Lucky", ("Daft Punk",), "RAM"),
    ])
    page.click("#results-details summary")
    page.fill("#results-q", "lucky")
    page.wait_for_function(
        "() => document.querySelectorAll('#results-rows tr').length === 1"
    )
    assert "Get Lucky" in page.locator("#results-rows").text_content()



# ------------------------------------------------------- playlists exclues


SOURCES_WITH_PLAYLISTS = """() => {
  excludedPlaylists = new Set(["PLdodo"]);
  $("sources-list").innerHTML =
    `<div class="source-group">${sourceRow({key: "library", label: "Bibliothèque"}, true)}</div>`
    + `<div class="source-group">${sourceRow({key: "playlist:PLdodo", label: "Berceuses"}, false)}`
    + `${sourceRow({key: "playlist:PLrock", label: "Rock"}, true)}</div>`;
  updateSourceTotal();
}"""


@pytest.mark.parametrize("reloads", [1, 2])
def test_reloading_the_sources_does_not_double_the_exclusion_toggle(page, reloads):
    """Régression : chaque chargement de la liste ajoutait un écouteur ; après
    une reconnexion, un clic basculait l'exclusion deux fois — sans effet.
    Deux nombres de rechargements, pour qu'un nombre pair d'écouteurs se
    présente quel que soit l'état initial."""
    page.evaluate(f"async () => {{ for (let i = 0; i < {reloads}; i++) await loadSources(); }}")
    page.evaluate(SOURCES_WITH_PLAYLISTS)
    page.click('.source[data-key="playlist:PLrock"] .exclude-btn')

    assert "excluded" in page.locator('.source[data-key="playlist:PLrock"]').get_attribute("class")


def test_an_excluded_playlist_is_shown_as_such(page):
    page.evaluate(SOURCES_WITH_PLAYLISTS)
    row = page.locator('.source[data-key="playlist:PLdodo"]')

    assert "excluded" in row.get_attribute("class")
    assert row.locator("input").is_disabled()
    assert "seront retirés" in row.text_content()
    assert "Ne plus exclure" in row.locator(".exclude-btn").text_content()
    assert "1 playlist(s) exclue(s)" in page.locator("#sources-total").text_content()


def test_only_playlists_can_be_excluded(page):
    """La bibliothèque entière ne s'exclut pas : il suffit de la décocher."""
    page.evaluate(SOURCES_WITH_PLAYLISTS)
    assert page.locator('.source[data-key="library"] .exclude-btn').count() == 0


def test_excluding_a_playlist_is_saved_at_once(page, repository):
    page.evaluate(SOURCES_WITH_PLAYLISTS)
    page.click('.source[data-key="playlist:PLrock"] .exclude-btn')
    page.wait_for_function(
        "() => document.querySelector('.source[data-key=\"playlist:PLrock\"] .exclude-btn')"
        ".textContent.includes('Ne plus')"
    )
    row = page.locator('.source[data-key="playlist:PLrock"]')
    assert not row.locator("input").is_checked()
    page.wait_for_function("() => !document.querySelector('#sources-msg').textContent")
    # Le serveur l'a enregistrée : elle vaudra pour l'analyse suivante.
    for _ in range(50):
        if "PLrock" in repository.excluded_playlists():
            break
        page.wait_for_timeout(50)
    assert set(repository.excluded_playlists()) == {"PLdodo", "PLrock"}


def test_check_all_does_not_bring_back_an_excluded_playlist(page):
    page.evaluate(SOURCES_WITH_PLAYLISTS)
    page.click("#sources-all")
    assert not page.locator('.source[data-key="playlist:PLdodo"] input').is_checked()


# ------------------------------------------------------------- déplacements

MOVE_PLAN = """
[[playlist]]
nom = "Jazz · Fusion"
styles = ["Fusion"]

[[playlist]]
nom = "Jazz · Jazz-funk"
styles = ["Jazz-Funk"]
"""


def move(page, button, target):
    """Déplace par le choix de playlist : ouvrir, puis cliquer la cible."""
    button.click()
    page.locator(f'#move-picker button.pick[data-target="{target}"]').click()


def picker_families(page) -> dict[str, list[str]]:
    """Ce que montre le choix de playlist : famille -> playlists, dans l'ordre."""
    return page.evaluate("""() => Object.fromEntries(
      [...document.querySelectorAll('#move-picker .family')].filter((f) => f.querySelector('h4'))
        .map((f) => [f.querySelector('h4').firstChild.textContent.trim(),
                     [...f.querySelectorAll('button.pick')].map((b) => b.querySelector('.name').textContent.trim())]))""")


def picker_specials(page) -> list[str]:
    return [t.strip() for t in
            page.locator("#move-picker button.pick.special").all_text_contents()]


def seed_jazz(repository, config, tmp_path):
    from ytmgc import verdicts
    from ytmgc.classifier import classify_tracks
    from ytmgc.models import Track
    from ytmgc.verdicts import Verdict, VerdictBook

    plan = tmp_path / "playlists.toml"
    plan.write_text(MOVE_PLAN, encoding="utf-8")
    config.taxonomy.playlists_file = str(plan)
    tracks = [
        Track("j1", "Chameleon", ("Herbie Hancock",)),
        Track("j2", "Watermelon Man", ("Herbie Hancock",)),
        Track("w1", "Birdland", ("Weather Report",)),
    ]
    repository.upsert_tracks(tracks)
    classify_tracks(tracks, repository, FakeDiscogs({}), config)
    verdicts.save(VerdictBook([
        Verdict("Herbie Hancock", "Chameleon", "Jazz", "Jazz-Funk", "Groovy", 0.9),
        Verdict("Herbie Hancock", "Watermelon Man", "Jazz", "Jazz-Funk", "Groovy", 0.9),
        Verdict("Weather Report", "Birdland", "Jazz", "Fusion", "Groovy", 0.9),
    ]), config.claude.verdicts_file)


def test_a_track_can_be_moved_from_the_preview(page, repository, config, tmp_path):
    """Le parcours réel : ouvrir une playlist, décocher un titre, en déplacer
    un autre — et retrouver la playlist ouverte, le titre toujours décoché."""
    from ytmgc import placements

    seed_jazz(repository, config, tmp_path)
    page.click("#preview-btn")
    funk = page.locator('#preview-list details.pl[data-key="plan/jazz-jazz-funk"]')
    funk.wait_for()
    funk.locator("summary .name").click()
    page.wait_for_selector('#preview-list details.pl[data-key="plan/jazz-jazz-funk"] li.track')
    funk.locator('.track-check[value="j2"]').uncheck()

    move(page, funk.locator('button.move[data-videos="j1"]'), "plan/jazz-fusion")
    page.wait_for_function(
        "() => document.querySelector('#preview-msg').textContent.includes('déplacé vers')"
    )

    assert [p.playlist for p in placements.load(config.taxonomy.placements_file)] == ["Jazz · Fusion"]
    funk = page.locator('#preview-list details.pl[data-key="plan/jazz-jazz-funk"]')
    assert funk.get_attribute("open") is not None
    assert not funk.locator('.track-check[value="j2"]').is_checked()
    assert "1 titres" in funk.locator(".count").text_content()

    fusion = page.locator('#preview-list details.pl[data-key="plan/jazz-fusion"]')
    assert "2 titres" in fusion.locator(".count").text_content()
    fusion.locator("summary .name").click()
    moved = fusion.locator("li.track", has_text="Chameleon")
    assert "déplacé" in moved.text_content()
    moved.locator("button.move").click()
    assert "Rendre aux règles" in picker_specials(page)


def test_the_move_menu_lists_the_other_playlists(page, repository, config, tmp_path):
    seed_jazz(repository, config, tmp_path)
    page.click("#preview-btn")
    fusion = page.locator('#preview-list details.pl[data-key="plan/jazz-fusion"]')
    fusion.wait_for()
    fusion.locator("summary .name").click()
    fusion.locator("li.track").first.wait_for()

    fusion.locator("button.move").first.click()
    assert picker_families(page) == {"Jazz": ["Jazz-funk"]}
    assert picker_specials(page) == ["＋ Nouvelle playlist…", "Ne ranger nulle part"]


WIDE_PLAN = MOVE_PLAN + """
[[playlist]]
nom = "Funk · Latin funk"
manuelle = true

[[playlist]]
nom = "Funk · Afrobeat"
styles = ["Afrobeat"]

[[playlist]]
nom = "Électro · Deep house"
styles = ["Deep House"]

[[playlist]]
nom = "Divers"
manuelle = true
"""


def open_picker_on_chameleon(page, repository, config, tmp_path):
    seed_jazz(repository, config, tmp_path)
    (tmp_path / "playlists.toml").write_text(WIDE_PLAN, encoding="utf-8")
    page.click("#preview-btn")
    funk = page.locator('#preview-list details.pl[data-key="plan/jazz-jazz-funk"]')
    funk.wait_for()
    funk.locator("summary .name").click()
    funk.locator('button.move[data-videos="j1"]').click()
    page.locator("#move-picker-search").wait_for()


def test_the_picker_groups_playlists_by_family(page, repository, config, tmp_path):
    """Familles par ordre alphabétique, les sans-famille à la fin ; la
    playlist actuelle du titre n'est pas proposée."""
    open_picker_on_chameleon(page, repository, config, tmp_path)
    assert picker_families(page) == {
        "Électro": ["Deep house"],
        "Funk": ["Afrobeat", "Latin funk"],
        "Jazz": ["Fusion"],
        "Autres": ["Divers"],
    }
    assert page.evaluate("document.activeElement.id") == "move-picker-search"


def test_the_picker_shows_each_playlist_size(page, repository, config, tmp_path):
    """Le nombre de titres de chaque playlist, à droite de son nom."""
    open_picker_on_chameleon(page, repository, config, tmp_path)
    sizes = dict(page.evaluate("""() => [...document.querySelectorAll('#move-picker button.pick:not(.special)')]
      .map((b) => [b.querySelector('.name').textContent, b.querySelector('.n').textContent])"""))
    assert sizes["Fusion"] == "1 titre"
    assert sizes["Latin funk"] == "0 titre"
    jazz = page.locator("#move-picker .family", has_text="Fusion").locator("h4")
    assert "1 playlist" in jazz.text_content()


def test_the_picker_search_ignores_case_and_accents(page, repository, config, tmp_path):
    open_picker_on_chameleon(page, repository, config, tmp_path)
    page.fill("#move-picker-search", "LATIN")
    assert picker_families(page) == {"Funk": ["Latin funk"]}
    page.fill("#move-picker-search", "electro")
    assert picker_families(page) == {"Électro": ["Deep house"]}
    # Le nom de la famille compte : « funk » montre toute la famille Funk.
    page.fill("#move-picker-search", "funk")
    assert list(picker_families(page)) == ["Funk"]
    assert picker_specials(page) == ["＋ Créer la playlist « funk »"]


def test_enter_moves_to_the_first_playlist_found(page, repository, config, tmp_path):
    from ytmgc import placements

    open_picker_on_chameleon(page, repository, config, tmp_path)
    page.fill("#move-picker-search", "latin")
    page.press("#move-picker-search", "Enter")
    page.wait_for_function(
        "() => document.querySelector('#preview-msg').textContent.includes('déplacé vers')"
    )
    assert not page.locator("#move-picker").is_visible()
    assert [p.playlist for p in placements.load(config.taxonomy.placements_file)] == [
        "Funk · Latin funk"]


def test_a_search_without_match_offers_to_create_it(page, repository, config, tmp_path):
    open_picker_on_chameleon(page, repository, config, tmp_path)
    page.fill("#move-picker-search", "Jazz · Dimanche matin")
    assert picker_families(page) == {}
    assert "Aucune playlist" in page.locator("#move-picker-list").text_content()
    page.press("#move-picker-search", "Enter")
    assert page.locator("#new-playlist").is_visible()
    assert page.input_value("#new-playlist-name") == "Jazz · Dimanche matin"


def test_escape_closes_the_picker_without_moving(page, repository, config, tmp_path):
    from ytmgc import placements

    open_picker_on_chameleon(page, repository, config, tmp_path)
    page.keyboard.press("Escape")
    assert not page.locator("#move-picker").is_visible()
    assert len(placements.load(config.taxonomy.placements_file)) == 0


def test_a_track_can_be_removed_from_every_playlist(page, repository, config, tmp_path):
    """« ✕ » : on n'en veut finalement pas. Le titre sort de sa playlist,
    sans recalcul, et rejoint les non rangés d'où on peut le rendre aux règles."""
    from ytmgc import placements

    seed_jazz(repository, config, tmp_path)
    page.click("#preview-btn")
    funk = page.locator('#preview-list details.pl[data-key="plan/jazz-jazz-funk"]')
    funk.wait_for()
    funk.locator("summary .name").click()
    previews = []
    page.on("request", lambda r: previews.append(r.url) if "/api/preview" in r.url else None)
    funk.locator('button.drop[data-videos="j1"]').click()
    page.wait_for_function(
        "() => document.querySelector('#preview-msg').textContent.includes('retiré des playlists')"
    )

    assert previews == []
    assert [p.playlist for p in placements.load(config.taxonomy.placements_file)] == ["(aucune)"]
    assert "1 titres" in funk.locator(".count").text_content()
    page.locator("#unsorted details.pl summary").click()
    kept_out = page.locator("#unsorted li.track", has_text="Chameleon")
    assert kept_out.locator("button.drop").count() == 0
    kept_out.locator("button.move").click()
    assert "Rendre aux règles" in picker_specials(page)


# ------------------------------------------------- relecture par Claude

REVIEW_ANSWER = """```
FICHIER 1 SUR 1
Herbie Hancock | Chameleon | Jazz · Fusion | longue jam électrique
Herbie Hancock | Watermelon Man | Jazz · Fusion | même énergie que Birdland
```"""


def import_review(page, repository, config, tmp_path):
    seed_jazz(repository, config, tmp_path)
    page.click("#preview-btn")
    page.locator("#claude-review summary").wait_for()
    page.click("#claude-review summary")
    page.fill("#cr-answer", REVIEW_ANSWER)
    page.click("#cr-import")
    page.locator("#proposals li.proposal").first.wait_for()


def test_claude_proposals_are_listed_by_playlist(page, repository, config, tmp_path):
    previews = []
    page.on("request", lambda r: previews.append(r.url) if "/api/preview" in r.url else None)
    import_review(page, repository, config, tmp_path)

    assert "2 proposition(s) ajoutée(s)" in page.text_content("#cr-msg")
    group = page.locator("#proposals .proposal-group")
    assert group.count() == 1
    assert "Jazz · Jazz-funk" in group.locator("h4").text_content()
    chameleon = group.locator("li.proposal", has_text="Chameleon")
    assert "Jazz · Fusion" in chameleon.locator(".proposal-to").text_content()
    assert "longue jam électrique" in chameleon.text_content()
    assert "2 proposition(s) à trancher" in page.text_content("#cr-count")
    # L'import n'a pas recalculé l'aperçu : une seule requête, celle du bouton.
    assert len(previews) == 1


def test_accepting_moves_and_keeping_validates(page, repository, config, tmp_path):
    from ytmgc import placements, verdicts
    from ytmgc.models import Track

    import_review(page, repository, config, tmp_path)
    row = page.locator("#proposals li.proposal", has_text="Chameleon")
    row.locator("button.accept").click()
    page.wait_for_function(
        "() => document.querySelector('#preview-msg').textContent.includes('déplacé')")
    assert [p.playlist for p in placements.load(config.taxonomy.placements_file)] == ["Jazz · Fusion"]
    assert "2 titres" in page.locator(
        '#preview-list details.pl[data-key="plan/jazz-fusion"] .count').text_content()

    page.locator("#proposals li.proposal", has_text="Watermelon").locator("button.keep").click()
    page.wait_for_function("() => !document.querySelector('#proposals li.proposal')")
    verdict = verdicts.load(config.claude.verdicts_file).for_track(
        Track("j2", "Watermelon Man", ("Herbie Hancock",)))
    assert verdict.source == verdicts.MANUAL
    assert len(placements.load(config.taxonomy.placements_file)) == 1


def test_the_review_files_can_be_prepared_and_downloaded(page, repository, config, tmp_path):
    seed_jazz(repository, config, tmp_path)
    page.click("#preview-btn")
    page.locator("#claude-review summary").wait_for()
    page.click("#claude-review summary")
    page.click("#cr-export")
    link = page.locator("#cr-files a")
    link.wait_for()
    assert link.text_content() == "playlists-1-sur-1.txt"
    assert "0 réponse(s) importée(s) sur 1" in page.text_content("#cr-export-msg")


def test_no_move_menu_outside_the_family_sort(page):
    render_sample_preview(page)
    page.locator("#preview-list details.pl").first.click()
    page.wait_for_selector("li.track")
    assert page.locator("button.move").count() == 0
    assert page.locator("button.drop").count() == 0
    assert page.locator("#claude-review").is_hidden()


# --------------------------------------------------------------- relecture


def seed_unsure(repository, config, tmp_path, extra_artists=0):
    """Deux titres peu sûrs de Herbie Hancock, un de Weather Report, et
    `extra_artists` artistes de plus, un titre chacun."""
    from ytmgc import verdicts
    from ytmgc.classifier import classify_tracks
    from ytmgc.models import Track
    from ytmgc.verdicts import Verdict, VerdictBook

    plan = tmp_path / "playlists.toml"
    plan.write_text(MOVE_PLAN, encoding="utf-8")
    config.taxonomy.playlists_file = str(plan)
    tracks = [
        Track("j1", "Chameleon", ("Herbie Hancock",)),
        Track("j2", "Watermelon Man", ("Herbie Hancock",)),
        Track("w1", "Birdland", ("Weather Report",)),
        *[Track(f"x{i}", f"Titre {i}", (f"Artiste {i:02d}",)) for i in range(extra_artists)],
    ]
    repository.upsert_tracks(tracks)
    classify_tracks(tracks, repository, FakeDiscogs({}), config)
    verdicts.save(VerdictBook([
        Verdict("Herbie Hancock", "Chameleon", "Jazz", "Jazz-Funk", "Groovy", 0.2, note="Supposé."),
        Verdict("Herbie Hancock", "Watermelon Man", "Jazz", "Jazz-Funk", "Groovy", 0.2),
        Verdict("Weather Report", "Birdland", "Jazz", "Fusion", "Groovy", 0.2),
        *[Verdict(f"Artiste {i:02d}", f"Titre {i}", "Jazz", "Fusion", "Calme", 0.1)
          for i in range(extra_artists)],
    ]), config.claude.verdicts_file)


def open_review(page):
    page.click("#preview-btn")
    page.wait_for_selector("#review details")
    page.click("#review details summary")


def test_unsure_tracks_are_grouped_by_artist_with_a_listen_link(page, repository, config, tmp_path):
    seed_unsure(repository, config, tmp_path)
    open_review(page)

    assert "3 titre(s) à vérifier" in page.locator("#review summary").text_content()
    groups = page.locator("#review .review-group")
    assert groups.count() == 2
    first = groups.nth(0)
    assert "Herbie Hancock" in first.locator("h4").text_content()
    assert "dans « Jazz · Jazz-funk »" in first.text_content() and "Supposé." in first.text_content()
    assert first.locator("a.play").first.get_attribute("href") == "https://music.youtube.com/watch?v=j1"
    # Un artiste à un seul titre n'a pas d'action groupée.
    assert groups.nth(1).locator(".row button.confirm").count() == 0


def test_confirming_an_artist_takes_it_out_of_the_review(page, repository, config, tmp_path):
    from ytmgc import verdicts

    seed_unsure(repository, config, tmp_path)
    open_review(page)
    page.locator("#review .review-group").nth(0).locator(".row button.confirm").click()
    page.wait_for_function(
        "() => document.querySelector('#preview-msg').textContent.includes('validé')"
    )

    sources = {v.title: v.source for v in verdicts.load(config.claude.verdicts_file)}
    assert sources == {"Chameleon": "manuel", "Watermelon Man": "manuel", "Birdland": "claude"}
    # La relecture reste ouverte, et ne montre plus que Weather Report.
    assert page.locator("#review details").get_attribute("open") is not None
    assert page.locator("#review .review-group").count() == 1
    assert "1 titre(s) à vérifier" in page.locator("#review summary").text_content()


def test_an_artist_can_be_moved_in_one_go(page, repository, config, tmp_path):
    from ytmgc import placements

    seed_unsure(repository, config, tmp_path)
    open_review(page)
    move(page, page.locator("#review .review-group").nth(0).locator(".row button.move"),
         "plan/jazz-fusion")
    page.wait_for_function(
        "() => document.querySelector('#preview-msg').textContent.includes('déplacés vers')"
    )

    moved = {p.title: p.playlist for p in placements.load(config.taxonomy.placements_file)}
    assert moved == {"Chameleon": "Jazz · Fusion", "Watermelon Man": "Jazz · Fusion"}
    assert "3 titres" in page.locator(
        '#preview-list details.pl[data-key="plan/jazz-fusion"] .count').text_content()


def test_a_long_review_is_shown_a_few_artists_at_a_time(page, repository, config, tmp_path):
    seed_unsure(repository, config, tmp_path, extra_artists=25)
    open_review(page)

    assert page.locator("#review .review-group").count() == 20
    page.click("#review-more")
    assert page.locator("#review .review-group").count() == 27
    assert page.locator("#review-more").count() == 0


# ------------------------------------------------------------- suggestions


def test_a_suggested_playlist_is_one_click_away_and_nothing_is_recomputed(
        page, repository, config, tmp_path):
    """Les playlists voisines sont des boutons sous le titre ; un clic déplace
    le titre sur place, sans redemander l'aperçu ni relire le compte."""
    from ytmgc import placements

    seed_jazz(repository, config, tmp_path)
    page.click("#preview-btn")
    funk = page.locator('#preview-list details.pl[data-key="plan/jazz-jazz-funk"]')
    funk.wait_for()
    funk.locator("summary .name").click()
    row = funk.locator("li.track", has_text="Chameleon")
    row.wait_for()
    funk.locator('.track-check[value="j2"]').uncheck()

    button = row.locator(".suggest button.go")
    assert button.all_text_contents() == ["Jazz · Fusion"]
    # Sous le titre, pas dans la case à cocher : cliquer ne décoche rien.
    assert row.locator("label .suggest").count() == 0

    previews = []
    page.on("request", lambda r: previews.append(r.url) if "/api/preview" in r.url else None)
    button.click()
    page.wait_for_function(
        "() => document.querySelector('#preview-msg').textContent.includes('déplacé vers')"
    )

    assert previews == []
    assert [p.playlist for p in placements.load(config.taxonomy.placements_file)] == ["Jazz · Fusion"]
    assert "2 titres" in page.locator(
        '#preview-list details.pl[data-key="plan/jazz-fusion"] .count').text_content()
    funk = page.locator('#preview-list details.pl[data-key="plan/jazz-jazz-funk"]')
    assert funk.get_attribute("open") is not None
    assert "1 titres" in funk.locator(".count").text_content()
    assert not funk.locator('.track-check[value="j2"]').is_checked()


def test_the_review_offers_suggestions_too(page, repository, config, tmp_path):
    seed_unsure(repository, config, tmp_path)
    open_review(page)
    row = page.locator("#review li.track", has_text="Birdland")
    assert row.locator(".suggest button.go").all_text_contents() == ["Jazz · Jazz-funk"]


# ------------------------------------------------------------ ordre figé


def seed_two_artists(repository, config, tmp_path):
    """Zed a trois titres peu sûrs, Abe deux : Zed passe en premier."""
    from ytmgc import verdicts
    from ytmgc.classifier import classify_tracks
    from ytmgc.models import Track
    from ytmgc.verdicts import Verdict, VerdictBook

    plan = tmp_path / "playlists.toml"
    plan.write_text(MOVE_PLAN, encoding="utf-8")
    config.taxonomy.playlists_file = str(plan)
    songs = [("z1", "Zed", "Un"), ("z2", "Zed", "Deux"), ("z3", "Zed", "Trois"),
             ("a1", "Abe", "Quatre"), ("a2", "Abe", "Cinq")]
    tracks = [Track(v, title, (artist,)) for v, artist, title in songs]
    repository.upsert_tracks(tracks)
    classify_tracks(tracks, repository, FakeDiscogs({}), config)
    verdicts.save(VerdictBook([
        Verdict(artist, title, "Jazz", "Fusion", "Calme", 0.2) for _, artist, title in songs
    ]), config.claude.verdicts_file)


def review_artists(page):
    return [h.split()[0] for h in page.locator("#review .review-group h4").all_text_contents()]


def test_the_review_order_does_not_move_while_validating(page, repository, config, tmp_path):
    """Régression : valider deux titres de Zed le faisait passer derrière Abe,
    et la liste bougeait sous les yeux."""
    seed_two_artists(repository, config, tmp_path)
    open_review(page)
    assert review_artists(page) == ["Zed", "Abe"]

    for _ in range(2):
        before = page.locator("#review li.track").count()
        zed = page.locator("#review .review-group", has=page.locator("h4", has_text="Zed"))
        zed.locator("li.track button.confirm").first.click()
        page.wait_for_function(
            f"() => document.querySelectorAll('#review li.track').length === {before - 1}"
        )

    assert review_artists(page) == ["Zed", "Abe"]

    # L'ordre tient aussi après un rechargement de la page.
    page.reload(wait_until="networkidle")
    open_review(page)
    assert review_artists(page) == ["Zed", "Abe"]


def test_each_mood_is_shown_as_a_coloured_chip(page, repository, config, tmp_path):
    seed_jazz(repository, config, tmp_path)
    page.click("#preview-btn")
    fusion = page.locator('#preview-list details.pl[data-key="plan/jazz-fusion"]')
    fusion.wait_for()
    fusion.locator("summary .name").click()
    chip = fusion.locator(".mood").first
    assert chip.get_attribute("data-mood") == "Groovy"


def test_the_step_bar_links_to_every_step(page):
    targets = page.locator(".stepnav a").evaluate_all("links => links.map(a => a.hash)")
    assert len(targets) == 7
    for target in targets:
        assert page.locator(target).count() == 1, target


# ------------------------------------------------------- nouvelle playlist


def test_a_new_playlist_can_be_created_from_the_move_menu(page, repository, config, tmp_path):
    """Un titre qui n'a sa place nulle part : on crée la playlist sur le
    moment, il y est rangé, sans recalcul de l'aperçu."""
    from ytmgc import placements

    seed_jazz(repository, config, tmp_path)
    page.click("#preview-btn")
    funk = page.locator('#preview-list details.pl[data-key="plan/jazz-jazz-funk"]')
    funk.wait_for()
    funk.locator("summary .name").click()
    row = funk.locator("li.track", has_text="Chameleon")
    row.wait_for()

    previews = []
    page.on("request", lambda r: previews.append(r.url) if "/api/preview" in r.url else None)
    move(page, row.locator("button.move"), "__new__")
    assert page.locator("#new-playlist").is_visible()
    page.fill("#new-playlist-name", "Jazz · Dimanche matin")
    page.fill("#new-playlist-desc", "Café et croissants")
    page.click("#new-playlist-create")
    page.wait_for_function(
        "() => document.querySelector('#preview-msg').textContent.includes('créée')"
    )

    assert not page.locator("#new-playlist").is_visible()
    assert previews == []
    made = page.locator('#preview-list details.pl[data-key="plan/jazz-dimanche-matin"]')
    assert "1 titres" in made.locator(".count").text_content()
    assert [p.playlist for p in placements.load(config.taxonomy.placements_file)] == [
        "Jazz · Dimanche matin"]
    # La nouvelle playlist est aussitôt proposée dans les autres menus.
    funk.locator("li.track").first.wait_for()
    funk.locator("button.move").first.click()
    assert "Dimanche matin" in picker_families(page)["Jazz"]


def test_a_taken_name_keeps_the_dialog_open_with_the_reason(page, repository, config, tmp_path):
    seed_jazz(repository, config, tmp_path)
    page.click("#preview-btn")
    page.locator("#new-playlist-btn").wait_for()
    page.click("#new-playlist-btn")
    page.fill("#new-playlist-name", "Jazz · Fusion")
    page.click("#new-playlist-create")
    page.wait_for_function(
        "() => document.querySelector('#new-playlist-msg').textContent.includes('existe déjà')"
    )
    assert page.locator("#new-playlist").is_visible()
