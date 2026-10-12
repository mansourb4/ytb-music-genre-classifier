"""Copie locale des playlists : un dossier par playlist, un fichier m4a par titre.

    ~/Music/Playlists/Électro/Deep techno/Artiste - Titre.m4a

« Famille · Nom » devient « Famille/Nom ». Chaque fichier porte dans ses
propriétés l'identifiant YouTube de son titre : c'est ce qui le relie à la
bibliothèque, quel que soit son nom. Quand un titre change de playlist, son
fichier est déplacé au lieu d'être téléchargé à nouveau ; un titre qui ne
figure plus dans aucune playlist part dans « _Hors playlists » plutôt qu'à la
corbeille.

Le téléchargement passe par yt-dlp, le moteur dont Stacher n'est qu'une
interface. Les propriétés visibles dans Apple Music (titre, artiste, album,
genre, regroupement) sont écrites par l'outil depuis la base, déjà propre, et
réécrites quand le classement change.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ytmgc.models import PlaylistPlan, Track

DEFAULT_ROOT = Path.home() / "Music" / "Playlists"
#: Titres qui ne sont plus dans aucune playlist : gardés, jamais supprimés.
OUT_OF_PLAYLISTS = "_Hors playlists"
#: Transit : un fichier y arrive nommé par son identifiant, y reçoit ses
#: propriétés, puis rejoint sa playlist. Interrompu, il y est retrouvé.
STAGING = ".en-cours"
FAILURES_FILE = "_échecs.txt"
EXTENSION = ".m4a"
#: Au-delà, YouTube bloque sans doute (« Sign in to confirm you're not a bot ») :
#: insister ne ferait qu'aggraver le blocage.
MAX_CONSECUTIVE_FAILURES = 5

#: Ancien nommage, « Artiste - Titre [videoId].m4a » : reconnu pour être renommé.
_ID_IN_NAME = re.compile(r" ?\[([A-Za-z0-9_-]{11})\]$")
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_MAX_STEM = 150

#: Pochette recadrée au carré : les vignettes YouTube sont en 16:9, l'image
#: de l'album occupant le centre.
_SQUARE_THUMBNAIL = (
    "ThumbnailsConvertor+FFmpeg_o:-c:v mjpeg -qmin 1 -qscale:v 1 "
    "-vf crop=\"'if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'\""
)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _clean(text: str) -> str:
    text = _UNSAFE.sub("-", _nfc(text))
    return re.sub(r"\s+", " ", text).strip().lstrip(".").strip()


def folder_for(playlist_name: str) -> Path:
    """« Électro · Deep techno » → Électro/Deep techno."""
    parts = [_clean(part) for part in playlist_name.split(" · ")]
    return Path(*[part for part in parts if part] or ["Sans nom"])


def stem_for(track: Track) -> str:
    stem = _clean(f"{track.artist} - {track.title}" if track.artist else track.title)
    return stem[:_MAX_STEM].rstrip() or "Sans titre"


def _unique(folder: Path, stem: str, taken: set[str]) -> Path:
    """Nom libre dans le dossier, « (2) » à la clé en cas d'homonyme.

    La comparaison ignore la casse et les accents décomposés, comme le
    système de fichiers du Mac.
    """
    candidate, n = stem, 1
    while (str(folder / candidate)).casefold() in taken:
        n += 1
        candidate = f"{stem} ({n})"
    taken.add(str(folder / candidate).casefold())
    return folder / f"{candidate}{EXTENSION}"


@dataclass(frozen=True, slots=True)
class Tags:
    """Propriétés écrites dans le fichier, telles qu'Apple Music les affiche."""

    title: str
    artist: str
    album: str | None
    #: Genre : la playlist, « Électro · Deep techno ».
    genre: str
    #: Regroupement : le style précis du titre, « Minimal Techno ».
    grouping: str | None = None


@dataclass(frozen=True, slots=True)
class Wanted:
    path: Path
    tags: Tags


def targets(
    plans: Iterable[PlaylistPlan],
    tracks: Mapping[str, Track],
    styles: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Wanted]:
    """Chemin et propriétés attendus pour chaque titre planifié.

    Un titre présent dans plusieurs playlists (selon le type de tri) n'a
    qu'un fichier : celui de la première playlist qui le contient.
    """
    styles = styles or {}
    wanted: dict[str, Wanted] = {}
    taken: set[str] = set()
    for plan in plans:
        folder = folder_for(plan.name)
        for video_id in plan.video_ids:
            track = tracks.get(video_id)
            if track is None or video_id in wanted:
                continue
            grouping = ", ".join(list(styles.get(video_id, ()))[:2]) or None
            wanted[video_id] = Wanted(
                _unique(folder, stem_for(track), taken),
                Tags(track.title, ", ".join(track.artists), track.album, plan.name, grouping),
            )
    return wanted


def existing(root: Path, read_id: Callable[[Path], str | None]) -> dict[str, Path]:
    """Fichiers déjà présents, par identifiant (chemins relatifs à `root`).

    L'identifiant se lit dans les propriétés du fichier ; à défaut, dans son
    nom (ancien nommage). Un fichier sans l'un ni l'autre n'est pas à l'outil :
    il est laissé tranquille. macOS rend les noms
    décomposés (« E » + accent) : ils sont recomposés pour se comparer aux
    chemins attendus.
    """
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.rglob(f"*{EXTENSION}")):
        relative = Path(_nfc(str(path.relative_to(root))))
        video_id = read_id(path)
        if video_id is None:
            match = _ID_IN_NAME.search(path.stem)
            if match:
                video_id = match.group(1)
        if video_id:
            found.setdefault(video_id, relative)
    return found


@dataclass(frozen=True, slots=True)
class Move:
    video_id: str
    source: Path
    target: Path


@dataclass(frozen=True, slots=True)
class LocalPlan:
    moves: tuple[Move, ...]
    downloads: tuple[tuple[str, Path], ...]
    up_to_date: int


def organise(wanted: Mapping[str, Wanted], present: Mapping[str, Path]) -> LocalPlan:
    """Ce qu'il faut déplacer et télécharger pour coller aux playlists."""
    moves: list[Move] = []
    downloads: list[tuple[str, Path]] = []
    up_to_date = 0
    for video_id, item in wanted.items():
        source = present.get(video_id)
        if source is None:
            downloads.append((video_id, item.path))
        elif source != item.path:
            moves.append(Move(video_id, source, item.path))
        else:
            up_to_date += 1
    aside = Path(OUT_OF_PLAYLISTS)
    taken = {
        p.with_suffix("").as_posix().casefold()
        for v, p in present.items() if v not in wanted and p.parts[0] == OUT_OF_PLAYLISTS
    }
    for video_id, source in present.items():
        if video_id in wanted or source.parts[0] == OUT_OF_PLAYLISTS:
            continue
        stem = _ID_IN_NAME.sub("", source.stem) if source.parts[0] != STAGING else video_id
        moves.append(Move(video_id, source, _unique(aside, stem, taken)))
    return LocalPlan(tuple(moves), tuple(downloads), up_to_date)


def apply_moves(root: Path, moves: Iterable[Move]) -> None:
    """Déplace en deux temps, par le dossier de transit : deux titres qui
    échangent leur nom ne s'écrasent pas."""
    moves = list(moves)
    staging = root / STAGING
    staging.mkdir(parents=True, exist_ok=True)
    emptied: set[Path] = set()
    for move in moves:
        transit = staging / f"{move.video_id}{EXTENSION}"
        if root / move.source != transit:
            os.replace(root / move.source, transit)
            emptied.add((root / move.source).parent)
    for move in moves:
        target = root / move.target
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging / f"{move.video_id}{EXTENSION}", target)
    emptied.add(staging)
    for folder in sorted(emptied, key=lambda p: len(p.parts), reverse=True):
        _remove_empty(folder, root)


def _remove_empty(folder: Path, root: Path) -> None:
    """Supprime les dossiers devenus vides (un .DS_Store ne compte pas)."""
    while folder != root and folder.is_dir():
        leftovers = [p for p in folder.iterdir() if p.name != ".DS_Store"]
        if leftovers:
            return
        for p in folder.iterdir():
            p.unlink()
        folder.rmdir()
        folder = folder.parent


# ------------------------------------------------------------- propriétés

#: Propriété maison : Apple Music l'ignore, l'outil s'en sert pour s'y retrouver.
_ID_KEY = "----:com.apple.iTunes:YouTube ID"
_TAG_KEYS = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
             "genre": "\xa9gen", "grouping": "\xa9grp"}


def _mp4(path: Path):
    try:
        from mutagen.mp4 import MP4
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise RuntimeError(
            "Le module mutagen manque : lance « pip install mutagen » puis recommence."
        ) from exc
    return MP4(path)


def read_id(path: Path) -> str | None:
    from mutagen import MutagenError

    try:
        values = (_mp4(path).tags or {}).get(_ID_KEY)
    except MutagenError:  # fichier abîmé (téléchargement coupé) : ignoré
        return None
    return bytes(values[0]).decode("utf-8") if values else None


def needs_tags(path: Path, video_id: str, tags: Tags) -> bool:
    current = _mp4(path).tags or {}
    for name, key in _TAG_KEYS.items():
        wanted = getattr(tags, name)
        if (current.get(key) or [None])[0] != wanted:
            return True
    return read_id(path) != video_id


def write_tags(path: Path, video_id: str, tags: Tags | None) -> None:
    """Écrit les propriétés, ou seulement l'identifiant si `tags` vaut None."""
    from mutagen.mp4 import MP4FreeForm

    audio = _mp4(path)
    if audio.tags is None:
        audio.add_tags()
    for name, key in _TAG_KEYS.items() if tags else ():
        value = getattr(tags, name)
        if value:
            audio.tags[key] = [value]
        else:
            audio.tags.pop(key, None)
    audio.tags[_ID_KEY] = [MP4FreeForm(video_id.encode("utf-8"))]
    audio.save()


def retag(
    root: Path,
    wanted: Mapping[str, Wanted],
    present: Mapping[str, Path],
    *,
    needs: Callable[[Path, str, Tags], bool] = needs_tags,
    tag: Callable[[Path, str, Tags | None], None] = write_tags,
    has_id: Callable[[Path], str | None] = read_id,
) -> int:
    """Met à jour les propriétés des fichiers présents, là où ils sont.

    À faire avant les déplacements : un fichier en transit porte ainsi déjà
    son identifiant, et reste reconnu si l'outil est interrompu.
    """
    changed = 0
    for video_id, relative in present.items():
        path = root / relative
        item = wanted.get(video_id)
        if item is not None:
            if needs(path, video_id, item.tags):
                tag(path, video_id, item.tags)
                changed += 1
        elif has_id(path) is None:
            tag(path, video_id, None)
    return changed


# ------------------------------------------------------------- yt-dlp

def _candidates(name: str) -> list[Path]:
    home = Path.home()
    places = [
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        home / ".stacher",
        home / "Library" / "Application Support" / "Stacher",
        home / "Library" / "Application Support" / "stacher",
        home / "Library" / "Application Support" / "Stacher7",
        home / "Library" / "Application Support" / "stacher7",
    ]
    found = [place / variant for place in places for variant in (name, f"{name}_macos")]
    for app in Path("/Applications").glob("Stacher*.app"):
        found.extend(app.rglob(name))
    return found


def find_tool(name: str, explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_dir():
            path = path / name
        return path if path.is_file() else None
    on_path = shutil.which(name)
    if on_path:
        return Path(on_path)
    for candidate in _candidates(name):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def command(
    ytdlp: Path, track: Track, destination: Path, *,
    ffmpeg: Path | None = None, cookies_browser: str | None = None,
) -> list[str]:
    stem = destination.name[: -len(EXTENSION)]
    cmd = [
        str(ytdlp),
        "--no-playlist", "--quiet", "--no-warnings", "--no-progress",
        "--format", "bestaudio[ext=m4a]/bestaudio",
        "--extract-audio", "--audio-format", "m4a",
        "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
        "--postprocessor-args", _SQUARE_THUMBNAIL,
        "--output", str(destination.parent / (stem.replace("%", "%%") + ".%(ext)s")),
    ]
    if ffmpeg is not None:
        cmd += ["--ffmpeg-location", str(ffmpeg)]
    if cookies_browser:
        cmd += ["--cookies-from-browser", cookies_browser]
    cmd.append(f"https://music.youtube.com/watch?v={track.video_id}")
    return cmd


@dataclass(slots=True)
class Report:
    downloaded: int = 0
    #: (libellé, dernière ligne d'erreur de yt-dlp)
    failed: list[tuple[str, str]] = field(default_factory=list)
    #: Arrêt anticipé : trop d'échecs d'affilée.
    stopped: bool = False


def download(
    root: Path,
    downloads: Iterable[tuple[str, Path]],
    tracks: Mapping[str, Track],
    wanted: Mapping[str, Wanted],
    ytdlp: Path,
    *,
    ffmpeg: Path | None = None,
    cookies_browser: str | None = None,
    pause: float = 2.0,
    on_progress: Callable[[int, int, Track, str | None], None] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    tag: Callable[[Path, str, Tags], None] = write_tags,
) -> Report:
    """Télécharge un titre après l'autre ; s'arrête si YouTube bloque.

    Le fichier arrive dans le dossier de transit, nommé par son identifiant,
    y reçoit ses propriétés, puis seulement rejoint sa playlist : un fichier
    rangé est toujours un fichier complet et reconnaissable.
    """
    todo = list(downloads)
    report = Report()
    consecutive = 0
    staging = root / STAGING
    for index, (video_id, relative) in enumerate(todo, 1):
        track = tracks[video_id]
        transit = staging / f"{video_id}{EXTENSION}"
        staging.mkdir(parents=True, exist_ok=True)
        # Reste d'une tentative coupée : yt-dlp le croirait déjà téléchargé.
        transit.unlink(missing_ok=True)
        result = run(
            command(ytdlp, track, transit, ffmpeg=ffmpeg, cookies_browser=cookies_browser),
            capture_output=True, text=True,
        )
        error = None
        if result.returncode != 0 or not transit.is_file():
            lines = (result.stderr or result.stdout or "").strip().splitlines()
            error = lines[-1] if lines else f"code de sortie {result.returncode}"
            report.failed.append((track.label(), error))
            consecutive += 1
        else:
            tag(transit, video_id, wanted[video_id].tags)
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(transit, destination)
            report.downloaded += 1
            consecutive = 0
        if on_progress:
            on_progress(index, len(todo), track, error)
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            report.stopped = True
            break
        if index < len(todo) and pause > 0:
            sleep(pause)
    _remove_empty(staging, root)
    return report


def write_failures(root: Path, failed: list[tuple[str, str]]) -> Path | None:
    path = root / FAILURES_FILE
    if not failed:
        if path.exists():
            path.unlink()
        return None
    path.write_text("".join(f"{label}\n    {error}\n" for label, error in failed),
                    encoding="utf-8")
    return path
