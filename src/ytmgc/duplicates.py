"""Doublons de la bibliothèque : un même morceau publié sous plusieurs formes.

YouTube Music présente un morceau comme titre d'album, comme clip, depuis la
chaîne « Topic » ou « VEVO » de l'artiste : autant d'identifiants pour une
seule chanson. Ils sont regroupés par `song_key` — c'est elle qui décide qu'un
morceau n'est jugé, et payé, qu'une fois.

Ce module ne fait que montrer ce regroupement. Il distingue :

  * les **doublons fusionnés** — même clé, donc même morceau pour l'outil ;
  * les **versions proches** — même artiste, un titre prolongeant l'autre
    (« Creep » et « Creep (Acoustic) »). Elles restent séparées à dessein,
    une version live ou acoustique n'ayant pas forcément l'ambiance de
    l'originale ; les lister permet d'en juger.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ytmgc.models import Track
from ytmgc.verdicts import track_key


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    key: str
    tracks: tuple[Track, ...]


@dataclass(frozen=True, slots=True)
class DuplicateReport:
    tracks: int
    distinct: int
    groups: list[DuplicateGroup]
    #: Paires (morceau, variante) gardées séparées.
    versions: list[tuple[Track, Track]]

    @property
    def merged(self) -> int:
        """Titres en trop : ceux qui ne coûteront rien, leur morceau étant déjà compté."""
        return self.tracks - self.distinct


def find_duplicates(tracks: list[Track]) -> DuplicateReport:
    by_key: dict[str, list[Track]] = defaultdict(list)
    for track in tracks:
        by_key[track_key(track)].append(track)

    groups = sorted(
        (DuplicateGroup(key, tuple(members)) for key, members in by_key.items() if len(members) > 1),
        key=lambda group: (-len(group.tracks), group.key),
    )
    return DuplicateReport(
        tracks=len(tracks),
        distinct=len(by_key),
        groups=groups,
        versions=_close_versions(by_key),
    )


def _close_versions(by_key: dict[str, list[Track]]) -> list[tuple[Track, Track]]:
    """Titres d'un même artiste dont l'un prolonge l'autre, mot pour mot.

    Comparer par artiste garde le calcul linéaire en pratique : une
    bibliothèque compte rarement plus de quelques dizaines de titres par
    artiste.
    """
    by_artist: dict[str, list[tuple[str, Track]]] = defaultdict(list)
    for key, members in by_key.items():
        artist, _, title = key.partition("::")
        if title:
            by_artist[artist].append((title, members[0]))

    pairs: list[tuple[Track, Track]] = []
    for songs in by_artist.values():
        songs.sort(key=lambda item: len(item[0]))
        for index, (short, original) in enumerate(songs):
            for long, variant in songs[index + 1:]:
                # Le préfixe doit s'arrêter sur une frontière de mot : « Creep »
                # prolongé par « Creep acoustic », pas « Creeping Death ».
                if long.startswith(short + " "):
                    pairs.append((original, variant))
    return sorted(pairs, key=lambda pair: (pair[0].artist.casefold(), pair[0].title.casefold()))
