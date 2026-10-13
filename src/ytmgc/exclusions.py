"""Playlists exclues : leurs titres sont retirés de l'analyse, d'où qu'ils viennent.

Ne pas cocher une playlist comme source ne suffit pas à l'écarter : ses titres
reviennent par la bibliothèque ou les likes. Une playlist de berceuses pour un
enfant, de bruit blanc pour dormir, n'a pourtant rien à faire dans le portrait
de ce qu'on écoute.

Un titre est retiré s'il figure dans une playlist exclue, ou si c'est le même
morceau publié autrement (`song_key`) : exclure le clip d'une comptine doit
aussi écarter sa version d'album.

Seule exception : un morceau que tu as toi-même placé dans une playlist
(data/placements.txt). Ce choix-là est explicite et l'emporte sur l'exclusion,
qui est un tri en gros.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection

from ytmgc.models import Track
from ytmgc.verdicts import track_key


@dataclass(frozen=True, slots=True)
class Scope:
    """Ce qui reste à analyser, et ce qui en a été retiré."""

    kept: list[Track]
    removed: list[Track]


def apply_exclusions(
    tracks: list[Track], excluded: list[Track], placed: Collection[str] = (),
) -> Scope:
    """`placed` : clés (`track_key`) des morceaux placés à la main, gardés."""
    if not excluded:
        return Scope(kept=list(tracks), removed=[])
    video_ids = {track.video_id for track in excluded}
    keys = {track_key(track) for track in excluded}
    placed = set(placed)
    kept: list[Track] = []
    removed: list[Track] = []
    for track in tracks:
        key = track_key(track)
        if key not in placed and (track.video_id in video_ids or key in keys):
            removed.append(track)
        else:
            kept.append(track)
    return Scope(kept=kept, removed=removed)


def excluded_tracks(client, playlist_ids: list[str]) -> list[Track]:
    """Les titres des playlists exclues, lus sur le compte."""
    if not playlist_ids:
        return []
    return client.scan([f"playlist:{playlist_id}" for playlist_id in playlist_ids])


def placed_keys(placements_file: str) -> set[str]:
    """Morceaux placés à la main dans une playlist (pas en « (aucune) »)."""
    from ytmgc import placements

    return {p.key for p in placements.load(placements_file) if not p.nowhere}
