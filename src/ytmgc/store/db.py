"""Connexion SQLite et schéma.

La base porte trois responsabilités :
  * l'inventaire de la bibliothèque scannée (reprise après interruption) ;
  * le cache des réponses Discogs (l'API est lente et limitée en débit) ;
  * les classifications, qui pilotent la planification des playlists.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    video_id    TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    artists     TEXT NOT NULL,          -- artistes séparés par " | "
    album       TEXT,
    duration_s  INTEGER,
    source      TEXT NOT NULL,
    scanned_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cache des recherches Discogs, indexé par requête normalisée plutôt que par
-- video_id : deux titres du même album ne consomment qu'un appel.
CREATE TABLE IF NOT EXISTS discogs_cache (
    query       TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,          -- JSON : liste de ReleaseCandidate
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS classifications (
    video_id      TEXT PRIMARY KEY REFERENCES tracks(video_id) ON DELETE CASCADE,
    status        TEXT NOT NULL,        -- matched | review | unmatched
    discogs_id    INTEGER,
    score         REAL NOT NULL DEFAULT 0,
    genres        TEXT NOT NULL DEFAULT '',
    styles        TEXT NOT NULL DEFAULT '',
    classified_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_classifications_status ON classifications(status);

-- Correspondance clé de playlist -> playlist YouTube Music réellement créée.
CREATE TABLE IF NOT EXISTS managed_playlists (
    key         TEXT PRIMARY KEY,
    playlist_id TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    synced_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Ouvre la base (en la créant si besoin) et applique le schéma."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    migrate(connection)
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()
