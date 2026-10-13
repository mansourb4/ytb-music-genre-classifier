"""Liker en série les titres d'une playlist YouTube Music.

Un like par titre, avec une pause entre deux : des milliers d'appels d'affilée
feraient limiter le compte. Les titres déjà likés sont sautés, si bien qu'une
relance reprend où la précédente s'est arrêtée.
"""

from __future__ import annotations

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
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False


def like_all(
    entries: Iterable[dict[str, Any]],
    like: Callable[[str], None],
    *,
    pause: float = 1.0,
    dry_run: bool = False,
    on_progress: Callable[[int, int, dict[str, Any], str | None], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    report = Report()
    todo = []
    seen: set[str] = set()
    for entry in entries:
        if entry["video_id"] in seen:
            continue
        seen.add(entry["video_id"])
        if entry["liked"]:
            report.already += 1
        else:
            todo.append(entry)
    if dry_run:
        report.liked = len(todo)
        return report

    consecutive = 0
    for index, entry in enumerate(todo, 1):
        error = None
        try:
            like(entry["video_id"])
            report.liked += 1
            consecutive = 0
        except Exception as exc:  # noqa: BLE001 - noté, et on continue
            error = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            report.failed.append((entry["label"], error))
            consecutive += 1
        if on_progress:
            on_progress(index, len(todo), entry, error)
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            report.stopped = True
            break
        if index < len(todo) and pause > 0:
            sleep(pause)
    return report
