"""Liker en série les titres d'une playlist YouTube Music.

Un like par titre, avec une pause entre deux : des milliers d'appels d'affilée
feraient limiter le compte. Les titres déjà likés sont sautés, si bien qu'une
relance reprend où la précédente s'est arrêtée.

YouTube Music range ses likes du plus récent au plus ancien : les titres sont
donc likés du bas de la playlist vers le haut, pour que son premier titre soit
le like le plus récent, et que « Titres likés » suive l'ordre de la playlist.
`reorder` le fait aussi pour les titres déjà likés : ils sont d'abord tous
dé-likés, puis tous re-likés dans cet ordre.
"""

from __future__ import annotations

import bisect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

#: Échecs d'affilée au-delà desquels on s'arrête : YouTube refuse sans doute
#: tout pour un moment, insister n'arrangerait rien.
MAX_CONSECUTIVE_FAILURES = 3


def find_playlists(summaries: Iterable[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Playlists dont le titre est `text`, ou à défaut le contient (casse ignorée)."""
    wanted = text.casefold().strip()
    summaries = list(summaries)
    exact = [s for s in summaries if s["title"].casefold().strip() == wanted]
    return exact or [s for s in summaries if wanted in s["title"].casefold()]


@dataclass(slots=True)
class Report:
    liked: int = 0
    already: int = 0
    #: Likes retirés avant d'être remis dans l'ordre (`reorder`).
    unliked: int = 0
    #: Titres retirés de YouTube, grisés dans la playlist : impossibles à liker.
    unavailable: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False


def like_all(
    entries: Iterable[dict[str, Any]],
    like: Callable[[str], None],
    *,
    unlike: Callable[[str], None] | None = None,
    reorder: bool = False,
    limit: int = 0,
    pause: float = 1.0,
    dry_run: bool = False,
    on_progress: Callable[[int, int, dict[str, Any], str | None, str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    """Like les titres de `entries` (dans l'ordre de la playlist).

    `limit` : seulement les N premiers titres de la playlist (pour tester).
    `on_progress` reçoit, en dernier, « unliked » ou « liked ».
    """
    report = Report()
    wanted = []
    seen: set[str] = set()
    for entry in entries:
        if not entry["video_id"]:
            report.unavailable += 1
            continue
        if entry["video_id"] in seen:
            continue
        seen.add(entry["video_id"])
        wanted.append(entry)
    if limit:
        wanted = wanted[:limit]
    if reorder:
        if unlike is None:
            raise ValueError("reorder demande de quoi retirer les likes")
        to_unlike = [entry for entry in wanted if entry["liked"]]
        todo = wanted
    else:
        to_unlike = []
        todo = [entry for entry in wanted if not entry["liked"]]
        report.already = len(wanted) - len(todo)
    # Du bas vers le haut : le premier titre de la playlist est liké en dernier.
    todo = todo[::-1]
    if dry_run:
        report.unliked = len(to_unlike)
        report.liked = len(todo)
        return report

    consecutive = 0
    total = len(to_unlike) + len(todo)
    steps = [(unlike, entry, "unliked") for entry in to_unlike]
    steps += [(like, entry, "liked") for entry in todo]
    for index, (action, entry, counter) in enumerate(steps, 1):
        error = None
        try:
            action(entry["video_id"])
            setattr(report, counter, getattr(report, counter) + 1)
            consecutive = 0
        except Exception as exc:  # noqa: BLE001 - noté, et on continue
            error = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            report.failed.append((entry["label"], error))
            consecutive += 1
        if on_progress:
            on_progress(index, total, entry, error, counter)
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            report.stopped = True
            break
        if index < total and pause > 0:
            sleep(pause)
    return report


@dataclass(slots=True)
class Check:
    """Une playlist face à « Titres likés »."""

    #: Titres disponibles de la playlist (sans doublon).
    wanted: int = 0
    #: Taille de « Titres likés », telle que l'API la rend.
    liked_total: int = 0
    #: Titres de la playlist absents de « Titres likés », dans l'ordre de la playlist.
    missing: list[dict[str, Any]] = field(default_factory=list)
    #: Titres présents des deux côtés, mais pas dans le même ordre relatif.
    out_of_order: int = 0
    #: Rang, dans « Titres likés », du premier titre de la playlist (0 = tout en haut).
    first_rank: int | None = None


def check(entries: Iterable[dict[str, Any]], liked: Iterable[dict[str, Any]]) -> Check:
    """Compare une playlist et « Titres likés » (du plus récent au plus ancien)."""
    result = Check()
    wanted, seen = [], set()
    for entry in entries:
        if entry["video_id"] and entry["video_id"] not in seen:
            seen.add(entry["video_id"])
            wanted.append(entry)
    result.wanted = len(wanted)
    order = [e["video_id"] for e in liked if e["video_id"]]
    result.liked_total = len(order)
    rank = {}
    for index, video_id in enumerate(order):
        rank.setdefault(video_id, index)
    result.missing = [e for e in wanted if e["video_id"] not in rank]
    present = [rank[e["video_id"]] for e in wanted if e["video_id"] in rank]
    if present:
        result.first_rank = present[0]
    # Dans l'ordre voulu, les rangs croissent : on compte les titres hors de la
    # plus longue suite croissante, ceux qu'il faudrait déplacer.
    tails: list[int] = []
    for value in present:
        at = bisect.bisect_left(tails, value)
        if at == len(tails):
            tails.append(value)
        else:
            tails[at] = value
    result.out_of_order = len(present) - len(tails)
    return result
