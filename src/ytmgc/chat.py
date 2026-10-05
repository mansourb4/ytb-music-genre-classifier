"""Passe gratuite : la bibliothèque jugée dans des conversations Claude.ai.

L'outil exporte d'un coup tous les titres pas encore jugés, en quelques
fichiers texte autonomes : consignes, vocabulaire, puis quelques centaines de
titres. Chaque fichier se glisse dans sa propre conversation Claude.ai, et la
réponse se recolle dans l'outil.

Pourquoi plusieurs fichiers plutôt qu'un seul : une conversation garde en
mémoire à la fois les titres *et* toutes les réponses. Pour quelques milliers
de titres, l'ensemble dépasse ce qu'elle peut tenir ; quelques centaines par
conversation y tiennent largement.

La réponse est une ligne par titre, au format même du fichier de verdicts :

    artiste | titre | genre | style | ambiance | confiance | note

Chaque ligne porte donc l'identité du morceau qu'elle juge. Il n'y a ni numéro
à suivre ni paquet à retrouver : une réponse se recolle dans n'importe quel
ordre, en autant de morceaux qu'on veut, des jours plus tard. Une ligne se
rattache à un morceau de la bibliothèque, ou n'est pas retenue — un verdict
écrit sous le mauvais titre serait tenu pour acquis et jamais redemandé.

Ce qui n'est pas retenu n'est pas perdu : le titre reste « à juger » et
figurera dans le prochain export.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ytmgc.matching.normalize import fold
from ytmgc.models import Track
from ytmgc.sources.claude import GUIDANCE, INTRO, RULES, Subject, build_reference, chunk
from ytmgc.taxonomy import Taxonomy
from ytmgc.verdicts import MODEL, Verdict, entry_key, main_artist, track_key

#: Nom des fichiers exportés : « titres-3-sur-9.txt » dit à lui seul où l'on en est.
FILE_RE = re.compile(r"^titres-\d+-sur-\d+\.txt$")
MANIFEST = "export.json"

COLUMNS = "artiste | titre | genre | style | ambiance | confiance | note"


class ChatError(ValueError):
    """Réponse inutilisable telle quelle, avec ce qu'il faut faire."""


# ------------------------------------------------------------------ export


def _instructions() -> str:
    fields = {
        "artiste, titre": (
            "recopiés exactement depuis la liste. C'est ce qui rattache ta\n"
            "  ligne au bon morceau : une ligne qui ne correspond à aucun titre est\n"
            "  ignorée."
        ),
        "genre": GUIDANCE["genre"],
        "style": GUIDANCE["style"],
        "ambiance": GUIDANCE["mood"],
        "confiance": GUIDANCE["confidence"],
        "note": GUIDANCE["note"],
    }
    return (
        INTRO
        + f"\nPour chaque titre de la liste, écris une ligne :\n\n    {COLUMNS}\n\n"
        + "".join(f"- {name} : {text}\n" for name, text in fields.items())
        + "\n"
        + RULES
        + "- Une ligne par titre, tous les titres, dans l'ordre de la liste.\n"
        + "- N'écris jamais le caractère « | » à l'intérieur d'un champ.\n"
        + "\nFORMAT DE LA RÉPONSE\n\n"
        + "Réponds uniquement par un bloc de code contenant les lignes, sans phrase\n"
        + "avant ni après. Si ta réponse ne tient pas en un message, arrête-toi à la\n"
        + "fin d'une ligne et ferme le bloc : on t'écrira « continue », et tu\n"
        + "reprendras au titre suivant, dans un nouveau bloc.\n"
    )


def _listing_line(subject: Subject) -> str:
    """« artiste | titre | indices » : les deux premières colonnes sont celles
    que la réponse doit recopier, le reste n'est qu'aide au jugement."""
    hints = []
    if subject.featuring:
        hints.append(f"avec {', '.join(subject.featuring)}")
    if subject.album:
        hints.append(f"album « {subject.album} »")
    if subject.year:
        hints.append(str(subject.year))
    if subject.genres or subject.styles:
        hints.append("Discogs : " + " / ".join((*subject.genres, *subject.styles)))
    if subject.tags:
        hints.append("Last.fm : " + ", ".join(subject.tags[:8]))
    # « | » sépare les colonnes : il ne peut pas figurer dans un champ.
    fields = (subject.artist, subject.title, " ; ".join(hints))
    return " | ".join(text.replace("|", "/") for text in fields)


def build_file(subjects: list[Subject], part: int, parts: int, taxonomy: Taxonomy) -> str:
    """Le contenu d'un fichier exporté, autonome : consignes comprises."""
    listing = "\n".join(_listing_line(subject) for subject in subjects)
    return "\n".join(
        (
            f"FICHIER {part} SUR {parts} — {len(subjects)} titres à juger",
            "",
            _instructions(),
            build_reference(taxonomy),
            "",
            "TITRES À JUGER (artiste | titre | indices)",
            "",
            listing,
            "",
        )
    )


@dataclass(frozen=True, slots=True)
class ExportFile:
    name: str
    path: Path
    #: Clés des morceaux du fichier, pour suivre ce qui en a été jugé.
    keys: tuple[str, ...]


def export_library(
    repository, config, taxonomy: Taxonomy, *, only_unsorted: bool = False
) -> list[ExportFile]:
    """Écrit tous les titres à juger, répartis en fichiers, d'un coup.

    Les fichiers d'un export précédent sont remplacés : ils contenaient des
    titres jugés depuis, et les garder ferait rejuger pour rien.
    """
    from ytmgc.enrich import pending_subjects
    from ytmgc.verdicts import load

    directory = Path(config.claude.export_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.iterdir():
        if FILE_RE.match(old.name) or old.name == MANIFEST:
            old.unlink()

    book = load(config.claude.verdicts_file)
    subjects = pending_subjects(repository, book, only_unsorted=only_unsorted)
    parts = chunk(subjects, config.claude.export_size)

    files: list[ExportFile] = []
    for number, part in enumerate(parts, start=1):
        name = f"titres-{number}-sur-{len(parts)}.txt"
        path = directory / name
        path.write_text(build_file(part, number, len(parts), taxonomy), encoding="utf-8")
        files.append(ExportFile(
            name=name, path=path,
            keys=tuple(entry_key(subject.artist, subject.title) for subject in part),
        ))

    (directory / MANIFEST).write_text(
        json.dumps({"files": [{"name": f.name, "keys": list(f.keys)} for f in files]},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return files


@dataclass(frozen=True, slots=True)
class FileProgress:
    name: str
    tracks: int
    judged: int

    @property
    def done(self) -> bool:
        return self.judged >= self.tracks


def export_progress(config) -> list[FileProgress]:
    """Avancement de chaque fichier du dernier export : ce qui en a été jugé."""
    from ytmgc.verdicts import load

    manifest = Path(config.claude.export_dir) / MANIFEST
    if not manifest.exists():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    book = load(config.claude.verdicts_file)
    return [
        FileProgress(
            name=item["name"],
            tracks=len(item["keys"]),
            judged=sum(1 for key in item["keys"] if key in book),
        )
        for item in data.get("files", [])
        if FILE_RE.match(str(item.get("name", "")))
    ]


def export_path(config, name: str) -> Path | None:
    """Chemin d'un fichier exporté, ou None si le nom n'en désigne pas un.

    Le nom vient d'une URL : il est validé contre le motif des fichiers
    exportés, de sorte qu'aucun autre fichier du disque ne soit atteignable.
    """
    if not FILE_RE.match(name):
        return None
    path = Path(config.claude.export_dir) / name
    return path if path.is_file() else None


# ---------------------------------------------------------------- lecture

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_SEPARATOR_RE = re.compile(r"^[\s|:\-–—]*$")
_FIRST_ARTIST_RE = re.compile(r"\s*(?:,|&|\bfeat\.?|\bft\.?|\bfeaturing\b|\bavec\b)\s*", re.I)


@dataclass(slots=True)
class Reading:
    """Ce qu'une réponse a donné, et ce qui en a été écarté."""

    verdicts: list[Verdict] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def _lines(text: str) -> list[str]:
    """Les lignes utiles d'une réponse collée.

    Une réponse longue arrive en plusieurs messages, souvent collés d'un coup :
    tous les blocs de code sont lus. Sans bloc, le texte entier l'est.
    """
    blocks = _FENCE_RE.findall(text)
    body = "\n".join(blocks) if blocks else text
    return [line.strip() for line in body.splitlines() if line.strip()]


def _fields(line: str) -> list[str] | None:
    if line.startswith("```") or _SEPARATOR_RE.match(line):
        return None
    # Tabulations si la réponse a été recopiée depuis le fichier de verdicts,
    # barres sinon ; une éventuelle mise en tableau Markdown est défaite.
    parts = line.split("\t") if "\t" in line else line.strip("|").split("|")
    fields = [part.strip() for part in parts]
    if len(fields) < 5 or fold(fields[0]) == "artiste":
        return None
    return fields


def _vocabulary(values: list[str]) -> dict[str, str]:
    """Correspondance tolérante vers le libellé exact : « melancolique » ->
    « Mélancolique ». La casse et les accents ne doivent pas coûter un verdict."""
    return {fold(value): value for value in values}


def _find(index: dict[str, Track], artist: str, title: str) -> Track | None:
    """Le morceau que désigne une ligne, ou None.

    Tolérant sur la forme — le bruit éditorial du titre, les invités ajoutés à
    l'artiste — jamais sur le fond : un autre morceau ne correspond pas.
    """
    track = index.get(entry_key(artist, title))
    if track is None:
        first = _FIRST_ARTIST_RE.split(artist, maxsplit=1)[0]
        if first != artist:
            track = index.get(entry_key(first, title))
    return track


def read_answer(text: str, tracks: list[Track], taxonomy: Taxonomy) -> Reading:
    """Lit une réponse collée, en ne gardant que les lignes vérifiables."""
    lines = _lines(text)
    if not lines:
        raise ChatError("Rien à lire : colle la réponse de Claude, ou dépose son fichier.")

    index = {track_key(track): track for track in tracks}
    genres = _vocabulary(taxonomy.genres())
    moods = _vocabulary(taxonomy.moods())
    reading = Reading()
    seen: set[str] = set()
    readable = 0

    for line in lines:
        fields = _fields(line)
        if fields is None:
            continue
        readable += 1
        artist, title, genre, style, mood = fields[:5]
        label = f"{artist} – {title}"

        track = _find(index, artist, title)
        if track is None:
            reading.rejected.append(f"{label} : aucun morceau de ce nom dans la bibliothèque")
            continue
        key = track_key(track)
        if key in seen:
            continue
        if fold(genre) not in genres:
            reading.rejected.append(f"{label} : genre « {genre} » hors de la liste")
            continue
        if fold(mood) not in moods:
            reading.rejected.append(f"{label} : ambiance « {mood} » hors de la liste")
            continue

        try:
            confidence = max(0.0, min(1.0, float(fields[5].replace(",", "."))))
        except (IndexError, ValueError):
            confidence = 0.0
        seen.add(key)
        reading.verdicts.append(Verdict(
            # L'orthographe de la bibliothèque, pas celle de la réponse : c'est
            # sous elle que le verdict sera retrouvé.
            artist=main_artist(track),
            title=track.title,
            genre=genres[fold(genre)],
            style=style,
            mood=moods[fold(mood)],
            confidence=confidence,
            source=MODEL,
            note=" | ".join(fields[6:]).strip(),
        ))

    if not readable:
        raise ChatError(
            "Aucune ligne au format « artiste | titre | genre | style | ambiance | … » "
            "dans ce texte. Copie la réponse avec le bouton « Copier » du bloc de code."
        )
    return reading


# --------------------------------------------------------- de bout en bout


@dataclass(slots=True)
class Imported:
    """Bilan d'une réponse collée."""

    accepted: int
    applied: int
    rejected: list[str]
    judged: int
    total: int

    @property
    def remaining(self) -> int:
        return self.total - self.judged

    def line(self) -> str:
        line = f"{self.accepted} verdict(s) enregistré(s)"
        if self.rejected:
            line += f", {len(self.rejected)} ligne(s) écartée(s)"
        return line + f". {self.judged} morceaux jugés sur {self.total}."


def import_answer(text: str, repository, config, taxonomy: Taxonomy) -> Imported:
    """Lit une réponse, écrit les verdicts retenus, et range la bibliothèque."""
    from ytmgc.enrich import apply_verdicts
    from ytmgc.verdicts import load, save

    tracks = repository.all_tracks()
    reading = read_answer(text, tracks, taxonomy)
    book = load(config.claude.verdicts_file)
    for verdict in reading.verdicts:
        book.add(verdict)
    if reading.verdicts:
        save(book, config.claude.verdicts_file)

    keys = {track_key(track) for track in tracks}
    return Imported(
        accepted=len(reading.verdicts),
        applied=apply_verdicts(repository, book, taxonomy, config),
        rejected=reading.rejected,
        judged=sum(1 for key in keys if key in book),
        total=len(keys),
    )
