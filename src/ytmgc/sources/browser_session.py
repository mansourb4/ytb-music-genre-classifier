"""Reprise d'une session YouTube Music déjà ouverte dans un navigateur.

Coller des en-têtes réseau est une manipulation d'ingénieur. Or l'information
nécessaire tient dans quelques cookies que le navigateur possède déjà : si tu
es connecté à YouTube Music dans Firefox ou Chrome, l'application peut
reprendre cette session sans aucune saisie.

Le fichier produit est celui qu'attend `ytmusicapi` en mode navigateur : un
dictionnaire d'en-têtes dont `cookie` est la pièce maîtresse. L'en-tête
`authorization` y figure aussi, non pour sa valeur — `ytmusicapi` la recalcule
à chaque requête à partir du cookie — mais parce que c'est à sa présence que la
bibliothèque reconnaît une authentification de type navigateur.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

#: Cookie dont `ytmusicapi` dérive la signature de chaque requête. Sans lui,
#: la session est inutilisable, quels que soient les autres cookies.
REQUIRED_COOKIE = "__Secure-3PAPISID"

#: Domaine des cookies de session YouTube.
DOMAIN = "youtube.com"

#: Navigateurs interrogeables, dans l'ordre de fiabilité observée.
BROWSERS = ("firefox", "chrome", "chromium", "brave", "edge", "vivaldi", "opera", "safari")

#: Navigateurs dont les cookies sont chiffrés par Chrome ≥ 127 sous Windows.
CHROMIUM_FAMILY = {"chrome", "chromium", "brave", "edge", "vivaldi", "opera"}


class BrowserSessionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Session:
    cookies: dict[str, str]
    source: str

    def header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items())


def cookies_from_jar(jar: Iterable[Any]) -> dict[str, str]:
    """Cookies YouTube d'un `CookieJar`, dernière valeur gagnante.

    Un même nom peut apparaître pour plusieurs domaines (`.youtube.com` et
    `music.youtube.com`) ; on ne garde que les cookies du domaine YouTube.
    """
    cookies: dict[str, str] = {}
    for cookie in jar:
        domain = getattr(cookie, "domain", "") or ""
        if DOMAIN in domain:
            cookies[cookie.name] = cookie.value
    return cookies


def build_headers(cookie: str, authuser: str = "0") -> dict[str, str]:
    """En-têtes complets attendus par `ytmusicapi`.

    Lève si le cookie déterminant est absent : mieux vaut un message clair
    ici qu'un échec obscur au premier appel d'API.
    """
    if REQUIRED_COOKIE not in cookie:
        raise BrowserSessionError(
            f"Cookie {REQUIRED_COOKIE} introuvable : la session n'est pas connectée "
            "à un compte Google. Ouvre music.youtube.com dans ce navigateur, "
            "connecte-toi, puis réessaie."
        )

    from ytmusicapi.helpers import get_authorization, initialize_headers, sapisid_from_cookie

    headers = dict(initialize_headers())
    headers["cookie"] = cookie
    headers["x-goog-authuser"] = authuser
    # Valeur horodatée, recalculée par ytmusicapi à chaque requête ; sa présence
    # est ce qui lui fait choisir le mode navigateur plutôt qu'OAuth.
    headers["authorization"] = get_authorization(
        sapisid_from_cookie(cookie) + " https://music.youtube.com"
    )
    return headers


def write_auth_file(cookie: str, path: str | Path, authuser: str = "0") -> None:
    headers = build_headers(cookie, authuser)
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(headers, indent=4, sort_keys=True), encoding="utf-8")
    file.chmod(0o600)


def _default_reader(browser: str | None) -> Any:
    """Lecture des cookies du navigateur installé, via `browser_cookie3`."""
    try:
        import browser_cookie3
    except ImportError as exc:  # pragma: no cover - dépendance optionnelle
        raise BrowserSessionError(
            "Cette méthode requiert une dépendance supplémentaire :\n"
            '  pip install "ytmgc[browser]"'
        ) from exc

    if browser is None:
        return browser_cookie3.load(domain_name=DOMAIN)
    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        raise BrowserSessionError(f"Navigateur inconnu : {browser}")
    return loader(domain_name=DOMAIN)


def _reader_failure(browser: str | None, exc: Exception) -> str:
    """Message adapté à la cause : navigateur absent, ou cookies inaccessibles.

    Les deux échecs sont fréquents et appellent des gestes différents ; les
    confondre laisserait l'utilisateur sans piste.
    """
    detail = str(exc)
    if "profile" in detail.lower() or "could not find" in detail.lower():
        cible = f"de {browser}" if browser else "de navigateur"
        return (
            f"Aucun profil {cible} trouvé sur cette machine. "
            "Choisis un navigateur que tu utilises réellement, ou une autre méthode."
        )
    if browser in CHROMIUM_FAMILY or browser is None:
        return (
            f"Lecture des cookies impossible : {detail}. "
            "Sous Windows, Chrome chiffre ses cookies depuis la version 127 : "
            "essaie Firefox, ou une autre méthode de connexion."
        )
    return f"Lecture des cookies impossible : {detail}."


def read_session(
    browser: str | None = None,
    reader: Callable[[str | None], Any] = _default_reader,
) -> Session:
    """Session YouTube trouvée dans le navigateur demandé, ou dans n'importe lequel."""
    try:
        jar = reader(browser)
    except BrowserSessionError:
        raise
    except Exception as exc:  # noqa: BLE001 - erreurs très variables selon l'OS
        raise BrowserSessionError(_reader_failure(browser, exc)) from exc

    cookies = cookies_from_jar(jar)
    if REQUIRED_COOKIE not in cookies:
        raise BrowserSessionError(
            "Aucune session YouTube connectée trouvée"
            + (f" dans {browser}." if browser else " dans les navigateurs installés.")
            + " Ouvre music.youtube.com, connecte-toi, puis réessaie."
        )
    return Session(cookies=cookies, source=browser or "navigateur détecté")


def import_session(
    path: str | Path,
    browser: str | None = None,
    reader: Callable[[str | None], Any] = _default_reader,
) -> str:
    """Reprend la session et écrit le fichier d'authentification. Renvoie la source."""
    session = read_session(browser, reader)
    write_auth_file(session.header(), path)
    return session.source


# ---------------------------------------------------------------------------
# Collage manuel : commande cURL ou en-têtes bruts
#
# « Copier comme cURL » est la seule action identique dans Firefox, Chrome,
# Edge et Safari : une entrée de menu, sans bouton caché ni panneau à déplier.
# On accepte donc les deux formes, et on n'exige qu'une chose du contenu — le
# cookie de session. Tout le reste est reconstruit.
# ---------------------------------------------------------------------------

_HEADER_OPTION_RE = re.compile(
    r"""(?:-H|--header)\s+(?P<quote>['"])(?P<value>.*?)(?P=quote)""", re.DOTALL
)
_COOKIE_OPTION_RE = re.compile(
    r"""(?:-b|--cookie)\s+(?P<quote>['"])(?P<value>.*?)(?P=quote)""", re.DOTALL
)


class PasteError(BrowserSessionError):
    """Collage inexploitable, avec le geste correctif à proposer."""


def _join_continuations(text: str) -> str:
    """Recolle les lignes d'une commande multiligne.

    Chaque interpréteur a sa marque de continuation : « \\ » pour bash,
    « ^ » pour cmd, « ` » pour PowerShell.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\\^`]\n\s*", " ", text)


def parse_pasted_headers(raw: str) -> dict[str, str]:
    """En-têtes contenus dans un collage, quelle qu'en soit la forme.

    Accepte une commande cURL (toutes variantes de guillemets) aussi bien
    qu'une simple liste « nom: valeur ».
    """
    text = _join_continuations(raw.strip())
    headers: dict[str, str] = {}

    if "curl" in text[:200].lower():
        for match in _HEADER_OPTION_RE.finditer(text):
            name, separator, value = match.group("value").partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        for match in _COOKIE_OPTION_RE.finditer(text):
            headers["cookie"] = match.group("value").strip()
        return headers

    for line in text.splitlines():
        name, separator, value = line.partition(":")
        # Une ligne de requête ("GET /youtubei/v1/... HTTP/2") n'est pas un en-tête.
        if separator and " " not in name.strip():
            headers[name.strip().lower()] = value.strip()
    return headers


def auth_file_from_paste(raw: str, path: str | Path) -> None:
    """Écrit le fichier d'authentification à partir d'un collage.

    Seul le cookie de session est réellement nécessaire : le reste des en-têtes
    est reconstruit. Cela rend n'importe quelle requête du domaine exploitable,
    là où exiger `x-goog-authuser` obligeait à trouver une requête d'API précise.
    """
    if "invoke-webrequest" in raw[:400].lower():
        raise PasteError(
            "Ce texte est une commande PowerShell. Dans le menu du navigateur, "
            "choisis « Copier comme cURL (bash) » plutôt que PowerShell."
        )

    headers = parse_pasted_headers(raw)
    cookie = headers.get("cookie")
    if not cookie:
        raise PasteError(
            "Aucun cookie trouvé dans le texte collé. La requête choisie n'était "
            "pas authentifiée : refais un clic droit sur une autre ligne de la "
            "liste, en étant connecté à ton compte."
        )
    if REQUIRED_COOKIE not in cookie:
        raise PasteError(
            f"Le cookie collé ne contient pas {REQUIRED_COOKIE} : cette requête "
            "n'est pas rattachée à ton compte. Vérifie que tu es bien connecté à "
            "YouTube Music, puis reprends une autre ligne."
        )

    write_auth_file(cookie, path, headers.get("x-goog-authuser", "0"))
