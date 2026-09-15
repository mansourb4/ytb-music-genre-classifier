"""Tags Last.fm, posés par les auditeurs sur un morceau précis.

Discogs étiquette des *releases* : les douze titres d'un album en héritent
identiquement, si bien qu'une ballade sur un disque punk se retrouve classée
« Punk ». Last.fm est la seule source encore ouverte qui décrive le *titre*
lui-même — ses tags disent « acoustic, ballad, mellow » là où la release dit
« Punk ».

L'API est gratuite mais demande une clé, et répond un titre à la fois. Le cache
en base est donc essentiel : la passe ne se paie qu'une fois.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ytmgc.config import LastfmConfig
from ytmgc.matching.normalize import normalize_artist, normalize_title
from ytmgc.models import Track
from ytmgc.ratelimit import TokenBucket

API_URL = "https://ws.audioscrobbler.com/2.0/"

#: Codes d'erreur Last.fm qu'il est inutile de réessayer.
NOT_FOUND = {6}


class LastfmError(RuntimeError):
    pass


def query_key(track: Track) -> str:
    """Clé de cache : l'artiste et le titre, pas l'album.

    C'est tout l'intérêt de cette source — deux titres d'un même disque n'ont
    aucune raison de partager leurs tags.
    """
    return f"{normalize_artist(track.artist)}::{normalize_title(track.title)}"


def parse_tags(payload: dict[str, Any]) -> list[tuple[str, int]]:
    """Extrait (tag, poids) d'une réponse.

    Last.fm renvoie `tag` tantôt comme une liste, tantôt comme un objet seul
    quand il n'y a qu'un résultat, et le poids tantôt en entier tantôt en
    chaîne. Le parseur absorbe les deux, une réponse mal formée devant donner
    « aucun tag » plutôt qu'une erreur.
    """
    if payload.get("error") is not None:
        return []

    raw = (payload.get("toptags") or {}).get("tag")
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]

    tags: list[tuple[str, int]] = []
    for item in raw:
        name = (item or {}).get("name")
        if not name:
            continue
        try:
            weight = int(item.get("count") or 0)
        except (TypeError, ValueError):
            weight = 0
        tags.append((str(name).strip(), weight))
    return tags


class LastfmClient:
    def __init__(
        self,
        config: LastfmConfig,
        *,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not config.api_key:
            raise LastfmError(
                "Clé Last.fm manquante : renseigne LASTFM_API_KEY (voir .env.example)"
            )
        if session is None:
            import requests  # import différé : dépendance optionnelle

            session = requests.Session()
        self._config = config
        self._session = session
        self._sleep = sleep
        self._bucket = TokenBucket(config.rate_limit_per_minute, sleep=sleep)

    def top_tags(self, track: Track) -> list[tuple[str, int]]:
        """Tags du morceau, du plus posé au moins posé.

        Un titre inconnu de Last.fm renvoie une liste vide : c'est un résultat,
        pas une panne, et il ne doit pas interrompre l'analyse.
        """
        params = {
            "method": "track.getTopTags",
            "artist": track.artist,
            "track": track.title,
            "api_key": self._config.api_key,
            "format": "json",
            # Corrige les fautes de frappe et les variantes de nom d'artiste,
            # fréquentes dans les libellés YouTube.
            "autocorrect": 1,
        }
        return parse_tags(self._get(params))

    def _get(self, params: dict[str, Any], *, attempts: int = 3) -> dict[str, Any]:
        headers = {"User-Agent": self._config.user_agent}
        delay = 2.0
        last_error: Exception | None = None

        for _ in range(attempts):
            self._bucket.acquire()
            try:
                response = self._session.get(API_URL, params=params, headers=headers, timeout=15)
            except Exception as exc:  # noqa: BLE001 - erreurs réseau hétérogènes
                last_error = exc
                self._sleep(delay)
                delay *= 2
                continue

            if response.status_code == 429:
                self._sleep(float(response.headers.get("Retry-After", delay)))
                delay *= 2
                continue
            if response.status_code == 403:
                raise LastfmError("Clé Last.fm refusée (403)")
            if response.status_code >= 500:
                last_error = LastfmError(f"Last.fm indisponible ({response.status_code})")
                self._sleep(delay)
                delay *= 2
                continue

            payload = response.json()
            if payload.get("error") in NOT_FOUND:
                return {}
            return payload

        raise LastfmError(f"Échec après {attempts} tentatives : {last_error}")
