"""Playlists exclues : leurs titres sont retirés de l'analyse, d'où qu'ils viennent.

Ne pas cocher une playlist comme source ne suffit pas à l'écarter : ses titres
reviennent par la bibliothèque ou les likes. Une playlist de berceuses pour un
enfant, de bruit blanc pour dormir, n'a pourtant rien à faire dans le portrait
de ce qu'on écoute.

Un titre est retiré s'il figure dans une playlist exclue, ou si c'est le même
morceau publié autrement (`song_key`) : exclure le clip d'une comptine doit
aussi écarter sa version d'album.
"""

from __future__ import annotations

from dataclasses import dataclass

from ytmgc.models import Track
from ytmgc.verdicts import track_key


@dataclass(frozen=True, slots=True)
class Scope:
    """Ce qui reste à analyser, et ce qui en a été retiré."""

    kept: list[Track]
    removed: list[Track]


def apply_exclusions(tracks: list[Track], excluded: list[Track]) -> Scope:
    if not excluded:
        return Scope(kept=list(tracks), removed=[])
    video_ids = {track.video_id for track in excluded}
    keys = {track_key(track) for track in excluded}
    kept: list[Track] = []
    removed: list[Track] = []
    for track in tracks:
        if track.video_id in video_ids or track_key(track) in keys:
            removed.append(track)
        else:
            kept.append(track)
    return Scope(kept=kept, removed=removed)


def excluded_tracks(client, playlist_ids: list[str]) -> list[Track]:
    """Les titres des playlists exclues, lus sur le compte."""
    if not playlist_ids:
        return []
    return client.scan([f"playlist:{playlist_id}" for playlist_id in playlist_ids])
