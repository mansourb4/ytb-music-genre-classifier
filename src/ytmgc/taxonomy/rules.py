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


@dataclass(frozen=True, slots=True)
class GenreStyle:
    """Couple (genre, style) normalisé, prêt à devenir une playlist."""

    genre: str
    style: str | None

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

    def describe_genre(self, genre: str) -> str | None:
        """Définition du genre, ou None s'il n'en existe pas encore."""
        return self._genre_descriptions.get(fold(genre))

    def describe_style(self, style: str) -> str | None:
        """Définition du style, ou None s'il n'en existe pas encore.

        Discogs compte plusieurs centaines de styles et en ajoute
        régulièrement : une absence est normale et ne doit rien casser.
        """
        return self._style_descriptions.get(fold(style))

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
    path: Path | None = None, descriptions_path: Path | None = None
) -> Taxonomy:
    data = tomllib.loads((path or DEFAULT_ALIASES).read_text(encoding="utf-8"))
    described = tomllib.loads(
        (descriptions_path or DEFAULT_DESCRIPTIONS).read_text(encoding="utf-8")
    )
    return Taxonomy(
        genre_display=data.get("genre_display", {}),
        genre_priority=data.get("genre_priority", {}).get("order", []),
        style_aliases=data.get("style_aliases", {}),
        style_blocklist=data.get("style_blocklist", {}).get("styles", []),
        genre_descriptions=described.get("genres", {}),
        style_descriptions=described.get("styles", {}),
    )


def playlist_name(item: GenreStyle, *, style_template: str, genre_template: str) -> str:
    if item.style:
        return style_template.format(genre=item.genre, style=item.style)
    return genre_template.format(genre=item.genre)
