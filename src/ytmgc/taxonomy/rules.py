"""Normalisation de la taxonomie Discogs et nommage des playlists.

YouTube Music n'offre aucune hiérarchie : la relation genre -> style est donc
encodée dans le *nom* de la playlist (« Electronic — Deep House »), de sorte
que le tri alphabétique de la bibliothèque regroupe les styles d'un même genre.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ytmgc.matching.normalize import fold, slugify

DEFAULT_ALIASES = Path(__file__).with_name("aliases.toml")
DEFAULT_DESCRIPTIONS = Path(__file__).with_name("descriptions.toml")
DEFAULT_MOODS = Path(__file__).with_name("moods.toml")

#: Genre porté par les playlists d'ambiance, pour qu'elles se regroupent dans
#: la bibliothèque comme les couples genre/style.
MOOD_GROUP = "Ambiance"


@dataclass(frozen=True, slots=True)
class GenreStyle:
    """Couple normalisé, prêt à devenir une playlist.

    Sert aux deux axes de rangement : (genre, style) pour la taxonomie Discogs,
    (Ambiance, humeur) pour le tri par ambiance. Tout ce qui suit — seuils,
    repli, nommage, description — opère indifféremment sur l'un ou l'autre.
    """

    genre: str
    style: str | None
    #: "style" ou "mood". Ne change que la façon de présenter le couple.
    axis: str = "style"

    @property
    def key(self) -> str:
        return f"{slugify(self.genre)}/{slugify(self.style)}" if self.style else slugify(self.genre)


class Taxonomy:
    """Applique la table d'alias à des valeurs brutes de l'API Discogs."""

    def __init__(
        self,
        genre_display: dict[str, str],
        genre_priority: list[str],
        style_aliases: dict[str, str],
        style_blocklist: list[str],
        genre_descriptions: dict[str, str] | None = None,
        style_descriptions: dict[str, str] | None = None,
        mood_descriptions: dict[str, str] | None = None,
        style_moods: dict[str, str] | None = None,
    ) -> None:
        self._genre_display = genre_display
        self._priority = {fold(name): rank for rank, name in enumerate(genre_priority)}
        self._style_aliases = {fold(key): value for key, value in style_aliases.items()}
        self._blocklist = {fold(name) for name in style_blocklist}

        # Les descriptions sont indexées sur le libellé Discogs brut ; on les
        # enregistre aussi sous le libellé affiché ("Funk / Soul" et
        # "Funk & Soul" désignent le même genre côté utilisateur).
        self._genre_descriptions: dict[str, str] = {}
        for raw, text in (genre_descriptions or {}).items():
            self._genre_descriptions[fold(raw)] = text
            self._genre_descriptions[fold(genre_display.get(raw, raw))] = text
        self._style_descriptions = {
            fold(name): text for name, text in (style_descriptions or {}).items()
        }
        self._mood_descriptions = {
            fold(name): text for name, text in (mood_descriptions or {}).items()
        }
        self._style_moods = {fold(name): mood for name, mood in (style_moods or {}).items()}
        self._mood_styles: dict[str, list[str]] = {}
        for name, mood in (style_moods or {}).items():
            self._mood_styles.setdefault(mood, []).append(name)

    def describe_genre(self, genre: str) -> str | None:
        """Définition du genre, ou None s'il n'en existe pas encore."""
        return self._genre_descriptions.get(fold(genre))

    def describe_style(self, style: str) -> str | None:
        """Définition du style, ou None s'il n'en existe pas encore.

        Discogs compte plusieurs centaines de styles et en ajoute
        régulièrement : une absence est normale et ne doit rien casser.
        """
        return self._style_descriptions.get(fold(style))

    def describe_mood(self, mood: str) -> str | None:
        return self._mood_descriptions.get(fold(mood))

    def styles_of_mood(self, mood: str) -> list[str]:
        """Styles rattachés à une ambiance, pour rendre le classement vérifiable."""
        return sorted(self._mood_styles.get(mood, []))

    def mood_of(self, style: str) -> str | None:
        """Ambiance d'un style, après application des alias. None si non rattaché."""
        canonical = self.canonical_style(style)
        return self._style_moods.get(fold(canonical)) if canonical else None

    def resolve_moods(self, styles: tuple[str, ...]) -> tuple[GenreStyle, ...]:
        """Ambiances d'une release, déduites de ses styles.

        Un genre seul ne dit rien de l'humeur — « Rock » recouvre aussi bien
        Shoegaze que Grindcore. Une release sans style exploitable ne relève
        donc d'aucune ambiance, et n'est rangée nulle part en mode ambiance.
        """
        resolved: list[GenreStyle] = []
        seen: set[str] = set()
        for style in styles:
            mood = self.mood_of(style)
            if mood is None or mood in seen:
                continue
            seen.add(mood)
            resolved.append(GenreStyle(MOOD_GROUP, mood, axis="mood"))
        return tuple(resolved)

    def display_genre(self, genre: str) -> str:
        return self._genre_display.get(genre, genre)

    def canonical_style(self, style: str) -> str | None:
        """Style après fusion des variantes ; None si le style est trop générique."""
        canonical = self._style_aliases.get(fold(style), style.strip())
        return None if fold(canonical) in self._blocklist else canonical

    def primary_genre(self, genres: tuple[str, ...]) -> str | None:
        """Genre principal d'une release, selon l'ordre de priorité configuré.

        Un genre absent de la liste de priorité passe après ceux qui y figurent,
        sans être écarté : Discogs peut introduire de nouvelles valeurs.
        """
        if not genres:
            return None
        ranked = sorted(
            genres,
            key=lambda genre: (self._priority.get(fold(genre), len(self._priority)), genres.index(genre)),
        )
        return ranked[0]

    def resolve(self, genres: tuple[str, ...], styles: tuple[str, ...]) -> tuple[GenreStyle, ...]:
        """Convertit la taxonomie brute d'une release en couples (genre, style).

        Sans genre exploitable, la fonction renvoie un tuple vide : le titre
        n'alimentera aucune playlist et sera compté comme non classé.
        """
        genre = self.primary_genre(genres)
        if genre is None:
            return ()
        display = self.display_genre(genre)

        resolved: list[GenreStyle] = []
        seen: set[str] = set()
        for style in styles:
            canonical = self.canonical_style(style)
            if canonical is None:
                continue
            if fold(canonical) in {fold(genre), fold(display)}:
                # Style homonyme de son genre (Discogs a par exemple le style
                # "Reggae" dans le genre "Reggae") : il n'ajoute aucune
                # information et donnerait une playlist "Reggae — Reggae".
                continue
            item = GenreStyle(display, canonical)
            if item.key not in seen:
                seen.add(item.key)
                resolved.append(item)

        return tuple(resolved) if resolved else (GenreStyle(display, None),)


def load_taxonomy(
    path: Path | None = None,
    descriptions_path: Path | None = None,
    moods_path: Path | None = None,
) -> Taxonomy:
    data = tomllib.loads((path or DEFAULT_ALIASES).read_text(encoding="utf-8"))
    described = tomllib.loads(
        (descriptions_path or DEFAULT_DESCRIPTIONS).read_text(encoding="utf-8")
    )
    moods = tomllib.loads((moods_path or DEFAULT_MOODS).read_text(encoding="utf-8"))
    return Taxonomy(
        genre_display=data.get("genre_display", {}),
        genre_priority=data.get("genre_priority", {}).get("order", []),
        style_aliases=data.get("style_aliases", {}),
        style_blocklist=data.get("style_blocklist", {}).get("styles", []),
        genre_descriptions=described.get("genres", {}),
        style_descriptions=described.get("styles", {}),
        mood_descriptions=moods.get("moods", {}),
        style_moods=moods.get("styles", {}),
    )


def playlist_name(item: GenreStyle, *, style_template: str, genre_template: str) -> str:
    if item.style:
        return style_template.format(genre=item.genre, style=item.style)
    return genre_template.format(genre=item.genre)
