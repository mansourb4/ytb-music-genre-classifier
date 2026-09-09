"""Accès typé à la base : le reste du code ne manipule jamais de SQL."""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar

from ytmgc.models import Classification, MatchStatus, ReleaseCandidate, Track

_SEP = " | "


def _join(values: tuple[str, ...]) -> str:
    return _SEP.join(values)


def _split(value: str) -> tuple[str, ...]:
    return tuple(part for part in value.split(_SEP) if part)


_T = TypeVar("_T")


def _locked(method: Callable[..., _T]) -> Callable[..., _T]:
    """Sérialise l'accès à la connexion SQLite.

    La connexion est partagée entre le fil qui sert l'interface et celui qui
    exécute les traitements longs ; sans ce verrou, les deux se marcheraient
    dessus au milieu d'une transaction.
    """

    @functools.wraps(method)
    def wrapper(self: "Repository", *args, **kwargs) -> _T:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Repository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- tracks

    @_locked
    def upsert_tracks(self, tracks: list[Track]) -> int:
        """Insère ou met à jour des titres. Renvoie le nombre de lignes traitées."""
        rows = [
            (t.video_id, t.title, _join(t.artists), t.album, t.duration_s, t.source, t.thumbnail)
            for t in tracks
        ]
        self._db.executemany(
            """
            INSERT INTO tracks(video_id, title, artists, album, duration_s, source, thumbnail)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title = excluded.title,
                artists = excluded.artists,
                album = excluded.album,
                duration_s = excluded.duration_s,
                source = excluded.source,
                thumbnail = COALESCE(excluded.thumbnail, tracks.thumbnail),
                scanned_at = datetime('now')
            """,
            rows,
        )
        self._db.commit()
        return len(rows)

    @_locked
    def all_tracks(self) -> list[Track]:
        cursor = self._db.execute("SELECT * FROM tracks ORDER BY video_id")
        return [self._row_to_track(row) for row in cursor]

    @_locked
    def unclassified_tracks(self) -> list[Track]:
        """Titres jamais soumis à Discogs : permet de reprendre un run interrompu."""
        cursor = self._db.execute(
            """
            SELECT t.* FROM tracks t
            LEFT JOIN classifications c ON c.video_id = t.video_id
            WHERE c.video_id IS NULL
            ORDER BY t.video_id
            """
        )
        return [self._row_to_track(row) for row in cursor]

    @staticmethod
    def _row_to_track(row: sqlite3.Row) -> Track:
        return Track(
            video_id=row["video_id"],
            title=row["title"],
            artists=_split(row["artists"]),
            album=row["album"],
            duration_s=row["duration_s"],
            source=row["source"],
            thumbnail=row["thumbnail"],
        )

    # ------------------------------------------------------- discogs cache

    @_locked
    def cached_candidates(self, query: str, ttl_days: int) -> list[ReleaseCandidate] | None:
        """Candidats en cache, ou None si absents ou périmés."""
        row = self._db.execute(
            "SELECT payload, fetched_at FROM discogs_cache WHERE query = ?", (query,)
        ).fetchone()
        if row is None:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"]).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched > timedelta(days=ttl_days):
            return None
        return [
            ReleaseCandidate(
                discogs_id=item["discogs_id"],
                title=item["title"],
                artist=item["artist"],
                year=item["year"],
                genres=tuple(item["genres"]),
                styles=tuple(item["styles"]),
                kind=item["kind"],
            )
            for item in json.loads(row["payload"])
        ]

    @_locked
    def store_candidates(self, query: str, candidates: list[ReleaseCandidate]) -> None:
        payload = json.dumps(
            [
                {
                    "discogs_id": c.discogs_id,
                    "title": c.title,
                    "artist": c.artist,
                    "year": c.year,
                    "genres": list(c.genres),
                    "styles": list(c.styles),
                    "kind": c.kind,
                }
                for c in candidates
            ],
            ensure_ascii=False,
        )
        self._db.execute(
            """
            INSERT INTO discogs_cache(query, payload) VALUES(?, ?)
            ON CONFLICT(query) DO UPDATE SET
                payload = excluded.payload,
                fetched_at = datetime('now')
            """,
            (query, payload),
        )
        self._db.commit()

    # ------------------------------------------------------ classifications

    @_locked
    def save_classification(self, classification: Classification) -> None:
        self._db.execute(
            """
            INSERT INTO classifications(video_id, status, discogs_id, score, genres, styles, year)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                status = excluded.status,
                discogs_id = excluded.discogs_id,
                score = excluded.score,
                genres = excluded.genres,
                styles = excluded.styles,
                year = excluded.year,
                classified_at = datetime('now')
            """,
            (
                classification.video_id,
                classification.status.value,
                classification.discogs_id,
                classification.score,
                _join(classification.genres),
                _join(classification.styles),
                classification.year,
            ),
        )
        self._db.commit()

    @_locked
    def classifications(self, status: MatchStatus | None = None) -> list[Classification]:
        if status is None:
            cursor = self._db.execute("SELECT * FROM classifications ORDER BY video_id")
        else:
            cursor = self._db.execute(
                "SELECT * FROM classifications WHERE status = ? ORDER BY video_id",
                (status.value,),
            )
        return [
            Classification(
                video_id=row["video_id"],
                status=MatchStatus(row["status"]),
                discogs_id=row["discogs_id"],
                score=row["score"],
                genres=_split(row["genres"]),
                styles=_split(row["styles"]),
                year=row["year"],
            )
            for row in cursor
        ]

    @_locked
    def counts_by_status(self) -> dict[str, int]:
        cursor = self._db.execute(
            "SELECT status, COUNT(*) AS n FROM classifications GROUP BY status"
        )
        return {row["status"]: row["n"] for row in cursor}

    # ---------------------------------------------------- managed playlists

    @_locked
    def remember_playlist(self, key: str, playlist_id: str, name: str) -> None:
        self._db.execute(
            """
            INSERT INTO managed_playlists(key, playlist_id, name) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                playlist_id = excluded.playlist_id,
                name = excluded.name,
                synced_at = datetime('now')
            """,
            (key, playlist_id, name),
        )
        self._db.commit()

    @_locked
    def forget_playlist(self, key: str) -> None:
        self._db.execute("DELETE FROM managed_playlists WHERE key = ?", (key,))
        self._db.commit()

    @_locked
    def managed_playlists(self) -> dict[str, tuple[str, str]]:
        """Clé de playlist -> (playlist_id, nom)."""
        cursor = self._db.execute("SELECT key, playlist_id, name FROM managed_playlists")
        return {row["key"]: (row["playlist_id"], row["name"]) for row in cursor}
