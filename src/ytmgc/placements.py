"""Déplacements faits à la main : un morceau mis dans une playlist du plan, quoi
qu'en disent les règles.

Les règles rangent par familles ; certaines oreilles en savent plus. Un
déplacement est le dernier mot sur la place d'un morceau, et il doit survivre à
tout ce qui recalcule : une nouvelle analyse, un changement de règle, une
nouvelle passe de jugement. Il vit donc, comme les verdicts, dans un fichier
texte versionnable — pas dans la base, qu'une analyse vide.

Un déplacement vise une playlist *par son nom*. Si le plan ne la contient plus
(renommée, supprimée), le déplacement est ignoré — le morceau retombe sous les
règles — et l'aperçu le signale plutôt que de le perdre sans un mot.

Format : `artiste <tab> titre <tab> playlist`. La playlist « (aucune) » tient
le morceau hors de toute playlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ytmgc.matching.normalize import slugify
from ytmgc.models import Track
from ytmgc.verdicts import entry_key, main_artist, track_key

#: Valeur qui tient un morceau hors de toute playlist.
NOWHERE = "(aucune)"

HEADER = """\
# Déplacements ytmgc — les morceaux que tu as mis toi-même dans une playlist.
#
# Une ligne par morceau, colonnes séparées par des tabulations :
#   artiste  |  titre  |  playlist
#
# Un déplacement l'emporte sur les règles de config/playlists.toml, et tient
# après chaque analyse. La playlist est désignée par son nom, tel qu'écrit
# dans le plan ; « (aucune) » tient le morceau hors de toute playlist.
# Supprime une ligne pour rendre le morceau aux règles.
"""


@dataclass(frozen=True, slots=True)
class Placement:
    artist: str
    title: str
    playlist: str

    @property
    def key(self) -> str:
        return entry_key(self.artist, self.title)

    @property
    def nowhere(self) -> bool:
        return self.playlist == NOWHERE

    @property
    def target(self) -> str | None:
        """Clé de la playlist visée, au format du plan ; None pour « (aucune) »."""
        return None if self.nowhere else "plan/" + slugify(self.playlist)

    def line(self) -> str:
        return "\t".join(
            field.replace("\t", " ").replace("\n", " ")
            for field in (self.artist, self.title, self.playlist)
        )


class PlacementBook:
    def __init__(self, placements: list[Placement] | None = None) -> None:
        self._by_key: dict[str, Placement] = {}
        for placement in placements or []:
            self._by_key[placement.key] = placement

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self):
        return iter(self._by_key.values())

    def get(self, key: str) -> Placement | None:
        return self._by_key.get(key)

    def put(self, track: Track, playlist: str) -> Placement:
        placement = Placement(main_artist(track), track.title, playlist)
        self._by_key[placement.key] = placement
        return placement

    def remove(self, track: Track) -> bool:
        return self._by_key.pop(track_key(track), None) is not None

    def lines(self) -> list[str]:
        ordered = sorted(self._by_key.values(), key=lambda p: (p.artist.casefold(), p.title.casefold()))
        return [placement.line() for placement in ordered]


def parse_line(line: str) -> Placement | None:
    """Une ligne, ou None si c'en est pas une : le fichier s'édite à la main."""
    stripped = line.strip("\n")
    if not stripped.strip() or stripped.lstrip().startswith("#"):
        return None
    parts = [part.strip() for part in stripped.split("\t")]
    if len(parts) < 3 or not all(parts[:3]):
        return None
    return Placement(*parts[:3])


def load(path: str | Path) -> PlacementBook:
    file = Path(path)
    if not file.exists():
        return PlacementBook()
    lines = file.read_text(encoding="utf-8").splitlines()
    return PlacementBook([p for p in (parse_line(line) for line in lines) if p is not None])


def save(book: PlacementBook, path: str | Path) -> int:
    """Réécrit le fichier entier, atomiquement, comme celui des verdicts."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(HEADER + "\n".join(book.lines()) + "\n", encoding="utf-8")
    temporary.replace(file)
    return len(book)


def library_overrides(tracks: list[Track], path: str | Path) -> dict[str, str | None]:
    """Les déplacements du fichier, prêts pour le plan."""
    return overrides(resolve(tracks, load(path)))


def overrides(resolved: dict[str, Placement]) -> dict[str, str | None]:
    """Forme attendue par le plan : identifiant vidéo -> clé de playlist ou None."""
    return {video_id: placement.target for video_id, placement in resolved.items()}


def resolve(tracks: list[Track], book: PlacementBook) -> dict[str, Placement]:
    """Déplacement de chaque titre de la bibliothèque, par identifiant vidéo.

    Le fichier est tenu par morceau : toutes les publications d'un même
    morceau suivent le même déplacement.
    """
    if not len(book):
        return {}
    resolved = {}
    for track in tracks:
        placement = book.get(track_key(track))
        if placement is not None:
            resolved[track.video_id] = placement
    return resolved
