"""Plan de playlists écrit à l'avance : chaque morceau rangé par une règle lisible.

Les autres types de tri déduisent les playlists des données : un style devient
une playlist dès qu'il atteint un seuil. Sur une grosse bibliothèque, cela
donne une centaine de playlists, un même style coupé entre plusieurs genres, et
des fourre-tout dont personne ne sait ce qu'ils contiennent.

Ici, c'est l'inverse. Le fichier `config/playlists.toml` dit quelles playlists
existent et ce qu'elle acceptent ; chaque morceau va dans la *première* dont une
règle lui correspond. Deux conséquences voulues :

  * l'ordre du fichier tranche les cas limites — une règle étroite (« Fusion
    groovy ») placée avant une large (« Fusion ») prend ce qui la concerne ;
  * un morceau qu'aucune règle n'accepte n'est versé dans aucun fourre-tout :
    il est montré, avec son genre, son style et son ambiance, pour qu'on
    ajoute la règle qui manque.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ytmgc.matching.normalize import fold, slugify
from ytmgc.models import Classification, MatchStatus, PlaylistPlan
from ytmgc.taxonomy import Taxonomy

#: Clés comprises dans un bloc `[[playlist]]`. Toute autre est une faute de
#: frappe : l'ignorer élargirait la règle sans prévenir.
_BLOCK_KEYS = {"nom", "description", "genres", "styles", "ambiances"}
_SIZE_KEYS = {"min", "max"}

#: YouTube Music refuse les chevrons dans une description de playlist.
_FORBIDDEN = str.maketrans({"<": "(", ">": ")"})


class PlanError(ValueError):
    """Fichier de plan illisible ou incohérent, avec la ligne à corriger."""


@dataclass(frozen=True, slots=True)
class Rule:
    """Une condition d'entrée. Un critère vide accepte tout ; les critères
    renseignés doivent tous être satisfaits."""

    genres: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    moods: tuple[str, ...] = ()
    _folded: tuple = field(init=False, repr=False, compare=False, default=())

    def __post_init__(self) -> None:
        # Repliés une fois pour toutes : chaque morceau essaie chaque règle.
        object.__setattr__(self, "_folded", (
            frozenset(fold(g) for g in self.genres),
            frozenset(fold(s) for s in self.styles),
            frozenset(fold(m) for m in self.moods),
        ))

    def matches(self, genres: set[str], styles: set[str], mood: str | None) -> bool:
        wanted_genres, wanted_styles, wanted_moods = self._folded
        if wanted_genres and not genres & wanted_genres:
            return False
        if wanted_styles and not styles & wanted_styles:
            return False
        if wanted_moods and mood not in wanted_moods:
            return False
        return True

    def describe(self) -> str:
        parts = []
        if self.genres:
            parts.append("genre " + " ou ".join(self.genres))
        if self.styles:
            parts.append("style " + ", ".join(self.styles))
        if self.moods:
            parts.append("ambiance " + ", ".join(self.moods))
        return " ; ".join(parts) or "tout morceau restant"


@dataclass(frozen=True, slots=True)
class PlannedPlaylist:
    name: str
    description: str
    rules: tuple[Rule, ...]

    @property
    def key(self) -> str:
        return "plan/" + slugify(self.name)


@dataclass(frozen=True, slots=True)
class Plan:
    playlists: tuple[PlannedPlaylist, ...]
    #: Taille visée : hors de cette plage, l'aperçu signale la playlist.
    min_size: int = 30
    max_size: int = 150

    def size_warning(self, count: int) -> str | None:
        if count > self.max_size:
            return f"grande ({count} titres, au-delà de {self.max_size}) : à découper"
        if count < self.min_size:
            return f"petite ({count} titres, en deçà de {self.min_size}) : à fusionner ?"
        return None

    def by_key(self) -> dict[str, PlannedPlaylist]:
        return {playlist.key: playlist for playlist in self.playlists}


@dataclass(frozen=True, slots=True)
class Features:
    """Ce qu'une règle examine d'un morceau, déjà replié pour la comparaison."""

    genres: set[str]
    styles: set[str]
    mood: str | None


# ------------------------------------------------------------------ lecture


def _names(block: dict, key: str, where: str) -> tuple[str, ...]:
    value = block.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PlanError(f"{where} : « {key} » doit être une liste de noms")
    return tuple(v.strip() for v in value)


def _check_vocabulary(names: tuple[str, ...], known: list[str], label: str, where: str) -> tuple[str, ...]:
    """Ramène chaque nom à l'orthographe du vocabulaire, ou refuse.

    Un genre ou une ambiance mal orthographié ne correspondrait à rien, et la
    règle se tairait : mieux vaut le dire au chargement.
    """
    by_fold = {fold(name): name for name in known}
    resolved = []
    for name in names:
        if fold(name) not in by_fold:
            raise PlanError(
                f"{where} : {label} « {name} » inconnu(e). Valeurs possibles : {', '.join(known)}"
            )
        resolved.append(by_fold[fold(name)])
    return tuple(resolved)


def parse_plan(text: str, taxonomy: Taxonomy, source: str = "playlists.toml") -> Plan:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PlanError(f"{source} illisible : {error}") from None

    unknown = set(data) - {"taille", "playlist"}
    if unknown:
        raise PlanError(f"{source} : section inconnue [{', '.join(sorted(unknown))}]")

    size = data.get("taille", {})
    if set(size) - _SIZE_KEYS:
        raise PlanError(f"{source} : [taille] n'accepte que min et max")
    min_size, max_size = size.get("min", 30), size.get("max", 150)
    if not (isinstance(min_size, int) and isinstance(max_size, int) and 0 <= min_size <= max_size):
        raise PlanError(f"{source} : [taille] demande deux entiers, min <= max")

    genres = taxonomy.genres()
    moods = taxonomy.moods()
    rules: dict[str, list[Rule]] = defaultdict(list)
    descriptions: dict[str, str] = {}
    order: list[str] = []

    for index, block in enumerate(data.get("playlist", []), start=1):
        name = str(block.get("nom", "")).strip()
        where = f"{source}, playlist n° {index}" + (f" « {name} »" if name else "")
        if not name:
            raise PlanError(f"{where} : « nom » manquant")
        if typo := set(block) - _BLOCK_KEYS:
            raise PlanError(
                f"{where} : clé inconnue « {', '.join(sorted(typo))} » "
                f"(attendu : {', '.join(sorted(_BLOCK_KEYS))})"
            )
        styles = tuple(
            style for style in (taxonomy.canonical_style(s) for s in _names(block, "styles", where))
            if style
        )
        rule = Rule(
            genres=_check_vocabulary(_names(block, "genres", where), genres, "genre", where),
            styles=styles,
            moods=_check_vocabulary(_names(block, "ambiances", where), moods, "ambiance", where),
        )
        # Deux blocs du même nom alimentent la même playlist : c'est ainsi
        # qu'on écrit « Jazz-Funk, ou bien Fusion quand elle est groovy ».
        if name not in rules:
            order.append(name)
        rules[name].append(rule)
        if description := str(block.get("description", "")).strip():
            descriptions.setdefault(name, description)

    if not order:
        raise PlanError(f"{source} ne décrit aucune playlist ([[playlist]] attendu)")

    playlists = tuple(PlannedPlaylist(n, descriptions.get(n, ""), tuple(rules[n])) for n in order)
    keys = [p.key for p in playlists]
    if len(set(keys)) != len(keys):
        raise PlanError(f"{source} : deux playlists ne diffèrent que par la casse ou les accents")
    return Plan(playlists, min_size=min_size, max_size=max_size)


def load_plan(path: str | Path, taxonomy: Taxonomy) -> Plan:
    file = Path(path)
    if not file.is_file():
        raise PlanError(
            f"Plan de playlists introuvable : {file}. Le tri « Par famille » s'appuie sur ce "
            "fichier ; copie config/playlists.toml depuis le dépôt ou change taxonomy.playlists_file."
        )
    return parse_plan(file.read_text(encoding="utf-8"), taxonomy, source=file.name)


# --------------------------------------------------------------- affectation


def usable(classification: Classification) -> bool:
    """Un morceau jugé par le modèle, ou apparié à Discogs, a de quoi être rangé."""
    return classification.judged or classification.status is MatchStatus.MATCHED


def features(classification: Classification, taxonomy: Taxonomy) -> Features:
    styles = (taxonomy.canonical_style(style) for style in classification.styles)
    return Features(
        genres={fold(taxonomy.display_genre(genre)) for genre in classification.genres},
        styles={fold(style) for style in styles if style},
        mood=fold(classification.mood) if classification.mood else None,
    )


def assign(classification: Classification, plan: Plan, taxonomy: Taxonomy) -> PlannedPlaylist | None:
    """La première playlist du plan qui accepte ce morceau, ou None."""
    if not usable(classification):
        return None
    seen = features(classification, taxonomy)
    for playlist in plan.playlists:
        if any(rule.matches(seen.genres, seen.styles, seen.mood) for rule in playlist.rules):
            return playlist
    return None


def build_description(playlist: PlannedPlaylist, marker: str) -> str:
    """Description écrite sur YouTube Music : ce qu'on y trouve, et pourquoi."""
    lines = [playlist.name, ""]
    if playlist.description:
        lines += [playlist.description, ""]
    lines.append("Règle : " + " — ou — ".join(rule.describe() for rule in playlist.rules) + ".")
    lines += ["", marker]
    return "\n".join(lines).translate(_FORBIDDEN)


def plan_by_rules(
    classifications: list[Classification],
    taxonomy: Taxonomy,
    plan: Plan,
    marker: str,
    overrides: Mapping[str, str | None] | None = None,
) -> list[PlaylistPlan]:
    """Les playlists du plan, dans l'ordre du fichier. Les vides sont omises.

    `overrides` associe un identifiant vidéo à la clé de la playlist choisie à
    la main, ou à None pour « nulle part ». Il l'emporte sur les règles — même
    pour un morceau qu'elles ne sauraient pas ranger. Une clé absente du plan
    est ignorée : le morceau retombe sous les règles.
    """
    playlists = plan.by_key()
    overrides = overrides or {}
    members: dict[str, list[str]] = defaultdict(list)
    for classification in classifications:
        video_id = classification.video_id
        if video_id in overrides and (overrides[video_id] is None or overrides[video_id] in playlists):
            target = overrides[video_id]
            if target is not None:
                members[target].append(video_id)
            continue
        playlist = assign(classification, plan, taxonomy)
        if playlist is not None:
            members[playlist.key].append(video_id)

    return [
        PlaylistPlan(
            key=playlist.key,
            name=playlist.name,
            description=build_description(playlist, marker),
            video_ids=tuple(members[playlist.key]),
            kind="plan",
        )
        for playlist in plan.playlists
        if members[playlist.key]
    ]
