"""Types de tri prédéfinis.

Les seuils et la politique multi-styles décident à eux seuls du résultat final,
mais ils ne parlent pas d'eux-mêmes. Ces quelques modes nommés les rendent
choisissables — en ligne de commande comme dans l'interface web — sans avoir à
comprendre le détail de la configuration.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from ytmgc.config import Config

#: Valeur de seuil qui désactive de fait les playlists de style.
NO_STYLE_PLAYLISTS = 1_000_000


@dataclass(frozen=True, slots=True)
class SortMode:
    key: str
    label: str
    summary: str
    multi_style: str
    max_styles_per_track: int
    min_tracks_per_style: int
    min_tracks_per_genre: int

    def describe(self) -> str:
        if self.min_tracks_per_style >= NO_STYLE_PLAYLISTS:
            granularity = "playlists par genre uniquement"
        else:
            granularity = f"un style obtient sa playlist à partir de {self.min_tracks_per_style} titres"
        placement = (
            f"un titre peut figurer dans {self.max_styles_per_track} playlists"
            if self.multi_style == "all"
            else "un titre ne figure que dans une playlist"
        )
        return f"{granularity} ; {placement}"


SORT_MODES: tuple[SortMode, ...] = (
    SortMode(
        key="detaille",
        label="Détaillé (recommandé)",
        summary="Une playlist par style, un titre pouvant relever de deux styles.",
        multi_style="all",
        max_styles_per_track=2,
        min_tracks_per_style=4,
        min_tracks_per_genre=3,
    ),
    SortMode(
        key="exhaustif",
        label="Exhaustif",
        summary="Le plus fin possible : chaque style représenté obtient sa playlist.",
        multi_style="all",
        max_styles_per_track=3,
        min_tracks_per_style=2,
        min_tracks_per_genre=2,
    ),
    SortMode(
        key="sans-doublon",
        label="Sans doublon",
        summary="Chaque titre n'apparaît que dans une seule playlist : une cartographie exacte.",
        multi_style="primary",
        max_styles_per_track=1,
        min_tracks_per_style=4,
        min_tracks_per_genre=3,
    ),
    SortMode(
        key="genre",
        label="Par genre",
        summary="Une poignée de grandes playlists, sans détail de style.",
        multi_style="primary",
        max_styles_per_track=1,
        min_tracks_per_style=NO_STYLE_PLAYLISTS,
        min_tracks_per_genre=3,
    ),
)

DEFAULT_SORT_MODE = SORT_MODES[0].key

_BY_KEY = {mode.key: mode for mode in SORT_MODES}


def get_sort_mode(key: str) -> SortMode:
    try:
        return _BY_KEY[key]
    except KeyError:
        known = ", ".join(_BY_KEY)
        raise ValueError(f"Type de tri inconnu : {key!r} (attendu : {known})") from None


def apply_sort_mode(config: Config, key: str) -> Config:
    """Copie la configuration en y appliquant le mode demandé.

    La copie évite qu'un aperçu calculé pour un mode ne modifie la
    configuration utilisée par les suivants — l'interface web en essaie
    plusieurs de suite sur une même session.
    """
    mode = get_sort_mode(key)
    updated = copy.deepcopy(config)
    updated.taxonomy.multi_style = mode.multi_style
    updated.taxonomy.max_styles_per_track = mode.max_styles_per_track
    updated.taxonomy.min_tracks_per_style = mode.min_tracks_per_style
    updated.taxonomy.min_tracks_per_genre = mode.min_tracks_per_genre
    updated.validate()
    return updated
