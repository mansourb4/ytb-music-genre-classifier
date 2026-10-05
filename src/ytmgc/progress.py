"""Où en est le jugement de la bibliothèque, morceau par morceau.

Répond aux trois questions qu'on se pose en cours de route : où vont les
résultats (le fichier de verdicts, chemin complet), ce qui est fait et ce qui
reste (chaque morceau, jugé ou non), et à quoi ressemble un résultat (le
verdict lui-même, note comprise).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ytmgc.matching.normalize import fold
from ytmgc.models import Track
from ytmgc.verdicts import VerdictBook, main_artist, track_key

SHOW = ("all", "done", "todo")


@dataclass(frozen=True, slots=True)
class Row:
    artist: str
    title: str
    album: str | None
    featuring: list[str]
    judged: bool
    genre: str | None = None
    style: str | None = None
    mood: str | None = None
    confidence: float | None = None
    note: str | None = None
    source: str | None = None


def rows(tracks: list[Track], book: VerdictBook) -> list[Row]:
    """Un rang par morceau distinct, trié par artiste puis titre.

    Par morceau et non par titre : c'est ainsi que les verdicts sont comptés,
    et les deux décomptes doivent concorder à l'écran.
    """
    seen: set[str] = set()
    result: list[Row] = []
    for track in tracks:
        key = track_key(track)
        if key in seen:
            continue
        seen.add(key)
        verdict = book.get(key)
        result.append(Row(
            artist=main_artist(track),
            title=track.title,
            album=track.album,
            featuring=list(track.artists[1:]),
            judged=verdict is not None,
            genre=verdict.genre if verdict else None,
            style=verdict.style if verdict else None,
            mood=verdict.mood if verdict else None,
            confidence=verdict.confidence if verdict else None,
            note=verdict.note if verdict else None,
            source=verdict.source if verdict else None,
        ))
    result.sort(key=lambda row: (row.artist.casefold(), row.title.casefold()))
    return result


def select(
    all_rows: list[Row], *, show: str = "all", query: str = ""
) -> list[Row]:
    """Filtre : jugés, à juger, et recherche sur l'artiste, le titre ou l'album."""
    if show == "done":
        all_rows = [row for row in all_rows if row.judged]
    elif show == "todo":
        all_rows = [row for row in all_rows if not row.judged]
    words = fold(query).split()
    if words:
        all_rows = [
            row for row in all_rows
            if all(word in fold(f"{row.artist} {row.title} {row.album or ''}") for word in words)
        ]
    return all_rows


def to_dict(row: Row) -> dict:
    return asdict(row)
