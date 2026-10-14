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
WAITS = (30.0, 60.0, 120.0, 300.0)
#: Le temps que « Titres likés » prenne en compte les derniers likes.
SETTLE = 3.0


@dataclass(slots=True)
class Reorder:
    unliked: int = 0
    #: Titres placés et vérifiés par ce passage.
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
    #: Titres qu'aucun essai n'a réussi à mettre à leur place.
    misplaced: list[dict[str, Any]] = field(default_factory=list)


def _in_place(top: list[str], done: list[str]) -> int:
    """Combien des premiers titres de `done` (likés dans cet ordre) sont déjà
    en tête de `top`, le dernier liké tout en haut."""
    for count in range(min(len(top), len(done)), 0, -1):
        if top[:count] == done[:count][::-1]:
            return count
    return 0


def _longest_in_order(got: list[str], expected: list[str]) -> set[str]:
    """Les titres de `got` qui forment la plus longue suite dans l'ordre de
    `expected` : les autres sont ceux à déplacer."""
    rank = {v: i for i, v in enumerate(expected)}
    items = [v for v in got if v in rank]
    tails: list[int] = []  # indice dans items de la fin de chaque suite
    parent = [-1] * len(items)
    for i, v in enumerate(items):
        at = bisect.bisect_left([rank[items[t]] for t in tails], rank[v])
        parent[i] = tails[at - 1] if at else -1
        if at == len(tails):
            tails.append(i)
        else:
            tails[at] = i
    keep: set[str] = set()
    i = tails[-1] if tails else -1
    while i >= 0:
        keep.add(items[i])
        i = parent[i]
    return keep


def reorder(
    entries: Iterable[dict[str, Any]],
    *,
    like: Callable[[str], None],
    unlike: Callable[[str], None],
    liked_ids: Callable[[int | None], list[str]],
    like_info: Callable[[str], tuple[bool, set[str]]],
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
    le haut, par lots de `batch`. Après chaque lot, le haut de « Titres
    likés » est relu : si YouTube a laissé tomber un like (ou un retrait), le
    lot est défait puis refait après une pause. « Titres likés » affiche
    parfois l'autre version d'un titre (clip au lieu d'audio, ou l'inverse) :
    `like_info` donne les deux, pour le reconnaître. Ce qu'aucun essai ne met
    en place est laissé de côté et signalé, sans arrêter le reste. Relancé
    après une interruption, il garde les titres déjà en place.
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

    infos: dict[str, tuple[bool, set[str]]] = {}
    #: Identifiant affiché dans « Titres likés » → titre de la playlist.
    shown_as: dict[str, str] = {}

    def info(video_id: str) -> tuple[bool, set[str]]:
        if video_id not in infos:
            infos[video_id] = like_info(video_id)
            for other in infos[video_id][1]:
                if other != video_id and other not in by_id:
                    shown_as.setdefault(other, video_id)
        return infos[video_id]

    def resolve(shown: str) -> str:
        return shown if shown in by_id else shown_as.get(shown, shown)

    def rest(seconds: float) -> None:
        if seconds > 0:
            sleep(seconds)

    # Reprise : les titres déjà likés dans l'ordre, en tête, restent.
    current = liked_ids(None)
    report.kept = _in_place([resolve(v) for v in current], ids)
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
            # Les mêmes, encore : le like porte peut-être sur l'autre version.
            for video_id in to_unlike:
                for other in sorted(info(video_id)[1] - {video_id}):
                    unlike(other)
                    report.unliked += 1
                    rest(pause)
            rest(settle)
            remaining = [v for v in liked_ids(None) if v in left]
            if remaining:
                # Ces likes-là ne se retirent pas : ils restent où ils sont,
                # hors de l'ordre ; les autres titres passent devant.
                for video_id in remaining:
                    report.stuck.append((by_id[video_id], info(video_id)[0]))
                on_line(f"{len(remaining)} like(s) impossibles à retirer : laissés à leur "
                        "place, on continue sans eux.")
                stuck = set(remaining)
                ids_left = [v for v in ids_left if v not in stuck]
            break
        previous = to_unlike
        report.retries += 1
        on_line(f"{len(to_unlike)} like(s) toujours là (YouTube a laissé tomber des "
                f"demandes) : pause de {waits[attempt]:.0f} s, puis nouvel essai.")
        rest(waits[attempt])

    # 2. Liker par lots, du bas vers le haut, en vérifiant chaque lot.
    #
    # Tout ce qui apparaît au-dessus des likes d'avant le lot vient du lot :
    # c'est ce « haut » qu'on compare à ce qui était attendu.
    before = set(liked_ids(None))
    hidden: set[str] = set()
    total = len(ids_left)

    def head(size: int) -> list[str]:
        top = liked_ids(2 * size + batch)
        for index, shown in enumerate(top):
            if shown in before:
                return top[:index]
        return top

    for start in range(0, total, batch):
        chunk = ids_left[start:start + batch]
        mine = set(chunk)
        signature = None
        for attempt in range(len(waits) + 1):
            for video_id in chunk:
                like(video_id)
                rest(pause)
            rest(settle)

            def got_from(top: list[str]) -> list[str]:
                found: list[str] = []
                for shown in top:
                    video_id = resolve(shown)
                    if video_id in mine and video_id not in found:
                        found.append(video_id)
                return found

            expected = [v for v in chunk[::-1] if v not in hidden]
            top = head(len(chunk))
            if got_from(top) != expected:
                rest(settle * 3)  # « Titres likés » est parfois en retard
                top = head(len(chunk))
            if got_from(top) != expected:
                # Un titre absent du haut : affiché sous son autre version, ou
                # liké mais jamais montré dans « Titres likés ».
                for video_id in expected:
                    if video_id not in got_from(top):
                        liked, versions = info(video_id)
                        if liked and not versions & set(top):
                            hidden.add(video_id)
                            report.hidden.append(by_id[video_id])
                expected = [v for v in chunk[::-1] if v not in hidden]
            got = got_from(top)
            before.update(top)
            if got == expected:
                report.liked += len(got)
                break
            if attempt == len(waits) or got == signature:
                # Même résultat deux fois : ce n'est plus du hasard, insister
                # n'y changera rien. On laisse ces titres et on continue.
                in_order = _longest_in_order(got, expected)
                report.misplaced.extend(by_id[v] for v in expected if v not in in_order)
                report.liked += len(in_order)
                on_line(f"  lot {start + 1}–{start + len(chunk)} : pas tout à fait en ordre, "
                        "laissé tel quel (détail à la fin).")
                break
            signature = got
            report.retries += 1
            on_line(f"  lot {start + 1}–{start + len(chunk)} : "
                    f"{len(expected) - len(got)} like(s) perdu(s), ou mal placé(s) : on défait "
                    f"le lot, pause de {waits[attempt]:.0f} s, puis on le refait.")
            for shown in top:
                if resolve(shown) in mine:
                    unlike(shown)
                    report.unliked += 1
                    rest(pause)
            for video_id in chunk:
                if video_id not in top:
                    unlike(video_id)
                    report.unliked += 1
                    rest(pause)
            before.difference_update(top)
            rest(waits[attempt])
        on_line(f"  [{start + len(chunk)}/{total}] likés et vérifiés")
    return report
