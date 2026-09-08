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


#: Lignes indispensables dans des en-têtes collés à la main.
PASTED_REQUIRED = ("cookie", "x-goog-authuser")


def missing_header_lines(raw: str) -> list[str]:
    """En-têtes obligatoires absents d'un collage manuel.

    L'erreur est presque toujours la même — une requête sans session choisie
    dans l'inspecteur réseau — et le message de `ytmusicapi` est en anglais.
    Ce contrôle permet de dire précisément ce qui manque et quoi refaire.
    """
    lines = [line.split(":", 1)[0].strip().lower() for line in raw.splitlines() if ":" in line]
    return [name for name in PASTED_REQUIRED if name not in lines]
