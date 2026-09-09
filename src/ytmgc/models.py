"""Objets de domaine partagés par toutes les couches.

Ces structures sont volontairement passives : la couche `sources` les produit,
`matching` / `taxonomy` les enrichissent, `planner` et `sync` les consomment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MatchStatus(str, Enum):
    """Issue de l'appariement d'un titre avec une release Discogs."""

    MATCHED = "matched"
    #: Un candidat existe mais son score est trop faible pour être appliqué seul.
    REVIEW = "review"
    #: Discogs ne renvoie rien d'exploitable.
    UNMATCHED = "unmatched"


@dataclass(frozen=True, slots=True)
class Track:
    """Un titre de la bibliothèque YouTube Music."""

    video_id: str
    title: str
    artists: tuple[str, ...]
    album: str | None = None
    duration_s: int | None = None
    #: "library", "liked", "uploads" ou "playlist:<id>".
    source: str = "library"
    #: Pochette fournie par YouTube Music, affichée telle quelle dans l'aperçu.
    thumbnail: str | None = None

    @property
    def artist(self) -> str:
        return self.artists[0] if self.artists else ""

    def label(self) -> str:
        return f"{', '.join(self.artists)} – {self.title}" if self.artists else self.title


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    """Une release Discogs renvoyée par la recherche."""

    discogs_id: int
    title: str
    #: Discogs renvoie un champ `title` de la forme "Artiste - Album" pour les
    #: releases ; `artist` porte la partie gauche une fois découpée.
    artist: str
    year: int | None
    genres: tuple[str, ...]
    styles: tuple[str, ...]
    #: "release" ou "master".
    kind: str = "release"


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: ReleaseCandidate
    score: float
    #: Détail des composantes du score, utile pour expliquer un appariement.
    breakdown: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Classification:
    """Résultat de l'appariement d'un titre, tel que persisté en base."""

    video_id: str
    status: MatchStatus
    discogs_id: int | None = None
    score: float = 0.0
    genres: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    #: Année de la release Discogs appariée.
    year: int | None = None


@dataclass(frozen=True, slots=True)
class PlaylistPlan:
    """Playlist souhaitée à l'issue de la planification."""

    #: Clé stable, indépendante du libellé affiché (ex. "electronic/deep-house").
    key: str
    name: str
    description: str
    video_ids: tuple[str, ...]
    #: "style", "genre" ou "fallback".
    kind: str = "style"
    #: Couple retenu, pour présenter la playlist autrement qu'en texte brut.
    genre: str | None = None
    style: str | None = None


@dataclass(frozen=True, slots=True)
class RemotePlaylist:
    """Playlist telle qu'elle existe côté YouTube Music."""

    playlist_id: str
    title: str
    description: str
    video_ids: tuple[str, ...] = ()
    #: Identifiants d'items nécessaires à la suppression (video_id -> setVideoId).
    set_video_ids: dict[str, str] = field(default_factory=dict)


class Op(str, Enum):
    CREATE = "create"
    ADD = "add"
    REMOVE = "remove"
    RENAME = "rename"


@dataclass(frozen=True, slots=True)
class SyncAction:
    """Une mutation à appliquer sur YouTube Music."""

    op: Op
    key: str
    playlist_name: str
    playlist_id: str | None = None
    video_ids: tuple[str, ...] = ()
    #: Renseigné pour RENAME.
    new_name: str | None = None

    def summary(self) -> str:
        if self.op is Op.CREATE:
            return f"créer « {self.playlist_name} » ({len(self.video_ids)} titres)"
        if self.op is Op.ADD:
            return f"ajouter {len(self.video_ids)} titre(s) à « {self.playlist_name} »"
        if self.op is Op.REMOVE:
            return f"retirer {len(self.video_ids)} titre(s) de « {self.playlist_name} »"
        return f"renommer « {self.playlist_name} » en « {self.new_name} »"
