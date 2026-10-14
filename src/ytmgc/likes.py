"""Liker en série les titres d'une playlist YouTube Music.

Un like par titre, avec une pause entre deux : des milliers d'appels d'affilée
feraient limiter le compte. Les titres déjà likés sont sautés, si bien qu'une
relance reprend où la précédente s'est arrêtée.

YouTube Music range ses likes du plus récent au plus ancien : les titres sont
donc likés du bas de la playlist vers le haut, pour que son premier titre soit
le like le plus récent, et que « Titres likés » suive l'ordre de la playlist.
`reorder` le fait aussi pour les titres déjà likés, en vérifiant le résultat :
YouTube répond « OK » à un like sans toujours l'enregistrer.
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
    #: Titres retirés de YouTube, grisés dans la playlist : impossibles à liker.
    unavailable: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False


def like_all(
    entries: Iterable[dict[str, Any]],
    like: Callable[[str], None],
    *,
    limit: int = 0,
    pause: float = 1.0,
    dry_run: bool = False,
    on_progress: Callable[[int, int, dict[str, Any], str | None], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    """Like les titres pas encore likés de `entries` (dans l'ordre de la playlist).

    `limit` : seulement les N premiers titres de la playlist (pour tester).
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
    todo = [entry for entry in wanted if not entry["liked"]]
    report.already = len(wanted) - len(todo)
    # Du bas vers le haut : le premier titre de la playlist est liké en dernier.
    todo = todo[::-1]
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
    #: Par tranche de `SLICE` titres de la playlist (de haut en bas) : combien manquent.
    missing_by_slice: list[tuple[int, int]] = field(default_factory=list)


#: Taille des tranches de `Check.missing_by_slice`.
SLICE = 250


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
    for start in range(0, len(wanted), SLICE):
        chunk = wanted[start:start + SLICE]
        result.missing_by_slice.append(
            (len(chunk), sum(1 for e in chunk if e["video_id"] not in rank)))
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


# ------------------------------------------------------- réordonner, vérifié

#: Titres likés d'affilée avant de vérifier qu'ils sont bien en tête.
BATCH = 25
#: Pauses (secondes) avant chaque nouvel essai, quand YouTube a laissé tomber
#: des likes ou des retraits : il répond « OK » sans toujours les enregistrer.
WAITS = (30.0, 60.0, 120.0, 300.0, 600.0)
#: Le temps que « Titres likés » prenne en compte les derniers likes.
SETTLE = 3.0


@dataclass(slots=True)
class Reorder:
    unliked: int = 0
    liked: int = 0
    unavailable: int = 0
    #: Déjà en place au départ (reprise d'un passage interrompu).
    kept: int = 0
    #: Lots refaits parce que YouTube avait laissé tomber des demandes.
    retries: int = 0
    #: Likés, mais que YouTube Music n'affiche pas dans « Titres likés »
    #: (vidéos qui ne sont pas des morceaux : DJ sets, extraits d'émission…).
    hidden: list[dict[str, Any]] = field(default_factory=list)
    #: Likes que YouTube refuse de retirer, laissés à leur place, avec le pouce
    #: tel que YouTube le donne (levé ou non) : (titre, pouce levé).
    stuck: list[tuple[dict[str, Any], bool]] = field(default_factory=list)
    #: Raison de l'arrêt, si l'ordre n'a pas pu être garanti.
    stopped: str | None = None


def _in_place(top: list[str], done: list[str]) -> int:
    """Combien des premiers titres de `done` (likés dans cet ordre) sont déjà
    en tête de `top`, le dernier liké tout en haut."""
    for count in range(min(len(top), len(done)), 0, -1):
        if top[:count] == done[:count][::-1]:
            return count
    return 0


def reorder(
    entries: Iterable[dict[str, Any]],
    *,
    like: Callable[[str], None],
    unlike: Callable[[str], None],
    liked_ids: Callable[[int | None], list[str]],
    is_liked: Callable[[str], bool],
    limit: int = 0,
    pause: float = 1.0,
    batch: int = BATCH,
    waits: tuple[float, ...] = WAITS,
    settle: float = SETTLE,
    on_line: Callable[[str], None] = lambda line: None,
    sleep: Callable[[float], None] = time.sleep,
) -> Reorder:
    """Met les likes de la playlist dans son ordre : son premier titre en tête
    de « Titres likés », et ainsi de suite.

    Les likes de la playlist sont d'abord tous retirés, puis remis du bas vers
    le haut, par lots de `batch`. Après chaque lot, « Titres likés » est relu :
    si YouTube a laissé tomber un like (ou un retrait), le lot est défait puis
    refait après une pause de plus en plus longue. Relancé après une
    interruption, il garde les titres déjà en place et reprend la suite.
    """
    report = Reorder()
    wanted, seen = [], set()
    for entry in entries:
        if not entry["video_id"]:
            report.unavailable += 1
        elif entry["video_id"] not in seen:
            seen.add(entry["video_id"])
            wanted.append(entry)
    if limit:
        wanted = wanted[:limit]
    todo = wanted[::-1]  # du bas vers le haut : le premier titre liké en dernier
    ids = [e["video_id"] for e in todo]
    by_id = {e["video_id"]: e for e in todo}

    def rest(seconds: float) -> None:
        if seconds > 0:
            sleep(seconds)

    # Reprise : les titres déjà likés dans l'ordre, en tête, restent.
    current = liked_ids(None)
    report.kept = _in_place(current, ids)
    if report.kept:
        on_line(f"{report.kept} titre(s) déjà en place en tête de « Titres likés » : "
                "on reprend après eux.")
    ids_left = ids[report.kept:]
    left = set(ids_left)

    # 1. Retirer les likes des titres restants, et vérifier qu'ils sont partis.
    to_unlike = [v for v in ids_left if by_id[v]["liked"]]
    flagged = set(to_unlike)
    to_unlike += [v for v in current if v in left and v not in flagged]
    previous: list[str] | None = None
    for attempt in range(len(waits) + 1):
        if to_unlike:
            on_line(f"Retrait de {len(to_unlike)} like(s)…")
            for index, video_id in enumerate(to_unlike, 1):
                unlike(video_id)
                report.unliked += 1
                if index % 100 == 0:
                    on_line(f"  {index}/{len(to_unlike)}")
                rest(pause)
            rest(settle)
        to_unlike = [v for v in liked_ids(None) if v in left]
        if not to_unlike:
            break
        if to_unlike == previous or attempt == len(waits):
            # Les mêmes, encore : ce n'est plus YouTube qui perd des demandes,
            # ces likes-là ne se retirent pas. Ils restent où ils sont, hors
            # de l'ordre ; les autres titres passent devant.
            for video_id in to_unlike:
                report.stuck.append((by_id[video_id], is_liked(video_id)))
            on_line(f"{len(to_unlike)} like(s) impossibles à retirer : laissés à leur place, "
                    "on continue sans eux.")
            stuck = set(to_unlike)
            ids_left = [v for v in ids_left if v not in stuck]
            break
        previous = to_unlike
        report.retries += 1
        on_line(f"{len(to_unlike)} like(s) toujours là (YouTube a laissé tomber des "
                f"demandes) : pause de {waits[attempt]:.0f} s, puis nouvel essai.")
        rest(waits[attempt])

    # 2. Liker par lots, du bas vers le haut, en vérifiant chaque lot.
    hidden: set[str] = set()
    total = len(ids_left)

    def head(count: int) -> list[str]:
        return [v for v in liked_ids(count + len(hidden)) if v not in hidden]

    for start in range(0, total, batch):
        chunk = ids_left[start:start + batch]
        for attempt in range(len(waits) + 1):
            for video_id in chunk:
                like(video_id)
                rest(pause)
            rest(settle)
            expect = chunk[::-1]
            top = head(len(expect) + batch)
            if top[:len(expect)] != expect:
                rest(settle * 3)  # « Titres likés » est parfois en retard
                top = head(len(expect) + batch)
            if top[:len(expect)] != expect:
                # Absents mais likés : YouTube Music ne les montre pas dans
                # « Titres likés ». On les laisse de côté pour l'ordre.
                for video_id in expect:
                    if video_id not in top and is_liked(video_id):
                        hidden.add(video_id)
                        report.hidden.append(by_id[video_id])
                expect = [v for v in expect if v not in hidden]
                top = [v for v in top if v not in hidden]
            if top[:len(expect)] == expect:
                report.liked += len(chunk)
                break
            if attempt == len(waits):
                report.stopped = ("YouTube laisse tomber des likes malgré "
                                  f"{len(waits)} nouveaux essais.")
                return report
            report.retries += 1
            dropped = [v for v in expect if v not in top]
            on_line(f"  lot {start + 1}–{start + len(chunk)} : {len(dropped)} like(s) "
                    f"perdu(s) ou mal placé(s) : on défait le lot, pause de "
                    f"{waits[attempt]:.0f} s, puis on le refait.")
            for video_id in chunk:
                if video_id in top or video_id in hidden:
                    unlike(video_id)
                    report.unliked += 1
                    rest(pause)
            rest(waits[attempt])
        on_line(f"  [{start + len(chunk)}/{total}] likés et vérifiés")
    return report
