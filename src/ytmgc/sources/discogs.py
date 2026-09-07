"""Client Discogs : recherche de releases et extraction de la taxonomie.

L'API impose 60 requêtes/minute avec jeton (25 sans). Le débit est donc le
facteur limitant du projet : le cache SQLite est indexé sur la requête
normalisée, ce qui fait qu'une bibliothèque de 5 000 titres tirés de 800 albums
ne coûte que ~800 appels, réutilisés d'un run à l'autre.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ytmgc.config import DiscogsConfig
from ytmgc.matching.normalize import normalize_artist, normalize_title, split_discogs_title
from ytmgc.models import ReleaseCandidate, Track
from ytmgc.ratelimit import TokenBucket

SEARCH_URL = "https://api.discogs.com/database/search"


class DiscogsError(RuntimeError):
    pass


def query_key(track: Track) -> str:
    """Clé de cache : deux titres du même artiste et du même album la partagent.

    Sans album on retombe sur le titre de la piste, ce qui reste correct mais
    moins mutualisable.
    """
    artist = normalize_artist(track.artist)
    anchor = normalize_title(track.album) if track.album else normalize_title(track.title)
    return f"{artist}::{anchor}"


def parse_results(payload: dict[str, Any], limit: int) -> list[ReleaseCandidate]:
    """Convertit la réponse brute de /database/search en candidats."""
    candidates: list[ReleaseCandidate] = []
    for item in (payload.get("results") or [])[:limit]:
        raw_title = item.get("title") or ""
        artist, album = split_discogs_title(raw_title)
        year = item.get("year")
        try:
            year_value = int(str(year)[:4]) if year else None
        except ValueError:
            year_value = None
        candidates.append(
            ReleaseCandidate(
                discogs_id=int(item.get("id", 0)),
                title=album or raw_title,
                artist=artist,
                year=year_value,
                genres=tuple(item.get("genre") or ()),
                styles=tuple(item.get("style") or ()),
                kind=item.get("type", "release"),
            )
        )
    return candidates


class DiscogsClient:
    def __init__(
        self,
        config: DiscogsConfig,
        *,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not config.token:
            raise DiscogsError(
                "Jeton Discogs manquant : renseigne DISCOGS_TOKEN (voir .env.example)"
            )
        if session is None:
            import requests  # import différé : dépendance optionnelle

            session = requests.Session()
        self._config = config
        self._session = session
        self._sleep = sleep
        self._bucket = TokenBucket(config.rate_limit_per_minute, sleep=sleep)

    def search(self, track: Track, *, limit: int = 10) -> list[ReleaseCandidate]:
        """Cherche les releases correspondant à un titre.

        La requête privilégie l'album : la taxonomie Discogs est portée par la
        release, et une recherche par album donne de bien meilleurs candidats
        qu'une recherche par piste isolée.
        """
        params: dict[str, Any] = {
            "artist": track.artist,
            "type": "release",
            "per_page": limit,
            "token": self._config.token,
        }
        if track.album:
            params["release_title"] = track.album
        else:
            params["track"] = track.title

        payload = self._get(params)
        return parse_results(payload, limit)

    def _get(self, params: dict[str, Any], *, attempts: int = 4) -> dict[str, Any]:
        headers = {"User-Agent": self._config.user_agent}
        delay = 2.0
        last_error: Exception | None = None

        for attempt in range(attempts):
            self._bucket.acquire()
            try:
                response = self._session.get(
                    SEARCH_URL, params=params, headers=headers, timeout=20
                )
            except Exception as exc:  # noqa: BLE001 - erreurs réseau hétérogènes
                last_error = exc
                self._sleep(delay)
                delay *= 2
                continue

            if response.status_code == 429:
                # Débit dépassé malgré le seau : on laisse la fenêtre se vider.
                self._sleep(float(response.headers.get("Retry-After", delay)))
                delay *= 2
                continue
            if response.status_code == 401:
                raise DiscogsError("Jeton Discogs refusé (401)")
            if response.status_code >= 500:
                last_error = DiscogsError(f"Discogs indisponible ({response.status_code})")
                self._sleep(delay)
                delay *= 2
                continue
            if response.status_code >= 400:
                raise DiscogsError(
                    f"Requête Discogs refusée ({response.status_code}) : {response.text[:200]}"
                )
            return response.json()

        raise DiscogsError(f"Échec après {attempts} tentatives : {last_error}")
