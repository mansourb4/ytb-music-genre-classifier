"""Copie locale des playlists : un dossier par playlist, un fichier m4a par titre.

    ~/Music/Playlists/Électro/Deep techno/Artiste - Titre [videoId].m4a

« Famille · Nom » devient « Famille/Nom ». L'identifiant YouTube, entre
crochets à la fin du nom de fichier, est ce qui relie un fichier à son titre :
quand un titre change de playlist, son fichier est déplacé au lieu d'être
téléchargé à nouveau, et un titre qui ne figure plus dans aucune playlist part
dans « _Hors playlists » plutôt qu'à la corbeille.

Le téléchargement passe par yt-dlp, le moteur dont Stacher n'est qu'une
interface. Les infos écrites dans le fichier (artiste, titre, album) viennent
de la base, déjà propres, et non du titre de la vidéo YouTube.
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
FAILURES_FILE = "_échecs.txt"
EXTENSION = ".m4a"
#: Au-delà, YouTube bloque sans doute (« Sign in to confirm you're not a bot ») :
#: insister ne ferait qu'aggraver le blocage.
MAX_CONSECUTIVE_FAILURES = 5

_ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]$")
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_MAX_STEM = 150

#: Pochette recadrée au carré : les vignettes YouTube sont en 16:9, l'image
#: de l'album occupant le centre.
_SQUARE_THUMBNAIL = (
    "ThumbnailsConvertor+FFmpeg_o:-c:v mjpeg -qmin 1 -qscale:v 1 "
    "-vf crop=\"'if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'\""
)


def _clean(text: str) -> str:
    text = _UNSAFE.sub("-", unicodedata.normalize("NFC", text))
    return re.sub(r"\s+", " ", text).strip().lstrip(".").strip()


def folder_for(playlist_name: str) -> Path:
    """« Électro · Deep techno » → Électro/Deep techno."""
    parts = [_clean(part) for part in playlist_name.split(" · ")]
    return Path(*[part for part in parts if part] or ["Sans nom"])


def file_name(track: Track) -> str:
    stem = _clean(f"{track.artist} - {track.title}" if track.artist else track.title)
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM].rstrip()
    return f"{stem or 'Sans titre'} [{track.video_id}]{EXTENSION}"


def targets(plans: Iterable[PlaylistPlan], tracks: Mapping[str, Track]) -> dict[str, Path]:
    """Chemin relatif attendu pour chaque titre planifié.

    Un titre présent dans plusieurs playlists (selon le type de tri) n'a
    qu'un fichier : celui de la première playlist qui le contient.
    """
    wanted: dict[str, Path] = {}
    for plan in plans:
        folder = folder_for(plan.name)
        for video_id in plan.video_ids:
            track = tracks.get(video_id)
            if track is not None and video_id not in wanted:
                wanted[video_id] = folder / file_name(track)
    return wanted


def existing(root: Path) -> dict[str, Path]:
    """Fichiers déjà présents, par identifiant (chemins relatifs à `root`).

    macOS rend les noms décomposés (« E » + accent) : ils sont recomposés pour
    se comparer aux chemins attendus, sans quoi chaque fichier accentué
    paraîtrait à déplacer.
    """
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.rglob(f"*{EXTENSION}")):
        match = _ID_IN_NAME.search(path.stem)
        if match:
            relative = unicodedata.normalize("NFC", str(path.relative_to(root)))
            found.setdefault(match.group(1), Path(relative))
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


def organise(wanted: Mapping[str, Path], present: Mapping[str, Path]) -> LocalPlan:
    """Ce qu'il faut déplacer et télécharger pour coller aux playlists."""
    moves: list[Move] = []
    downloads: list[tuple[str, Path]] = []
    up_to_date = 0
    for video_id, target in wanted.items():
        source = present.get(video_id)
        if source is None:
            downloads.append((video_id, target))
        elif source != target:
            moves.append(Move(video_id, source, target))
        else:
            up_to_date += 1
    for video_id, source in present.items():
        if video_id not in wanted and source.parts[0] != OUT_OF_PLAYLISTS:
            moves.append(Move(video_id, source, Path(OUT_OF_PLAYLISTS) / source.name))
    return LocalPlan(tuple(moves), tuple(downloads), up_to_date)


def apply_moves(root: Path, moves: Iterable[Move]) -> None:
    emptied: set[Path] = set()
    for move in moves:
        target = root / move.target
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(root / move.source, target)
        emptied.add((root / move.source).parent)
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


def _literal(field: str, value: str) -> list[str]:
    """Impose une valeur à une métadonnée, via --parse-metadata.

    Le préfixe `%(id)s|` empêche yt-dlp de prendre un mot seul (« Moderat »)
    pour un nom de champ ; `%` et `:` doivent être échappés.
    """
    escaped = value.replace("%", "%%").replace(":", "\\:")
    return ["--parse-metadata", f"%(id)s|{escaped}:^[^|]*\\|(?P<meta_{field}>.+)$"]


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
    cmd += _literal("title", track.title)
    if track.artists:
        cmd += _literal("artist", ", ".join(track.artists))
    if track.album:
        cmd += _literal("album", track.album)
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
    ytdlp: Path,
    *,
    ffmpeg: Path | None = None,
    cookies_browser: str | None = None,
    pause: float = 2.0,
    on_progress: Callable[[int, int, Track, str | None], None] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    """Télécharge un titre après l'autre ; s'arrête si YouTube bloque."""
    todo = list(downloads)
    report = Report()
    consecutive = 0
    for index, (video_id, relative) in enumerate(todo, 1):
        track = tracks[video_id]
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = run(
            command(ytdlp, track, destination, ffmpeg=ffmpeg, cookies_browser=cookies_browser),
            capture_output=True, text=True,
        )
        error = None
        if result.returncode != 0 or not destination.is_file():
            lines = (result.stderr or result.stdout or "").strip().splitlines()
            error = lines[-1] if lines else f"code de sortie {result.returncode}"
            report.failed.append((track.label(), error))
            consecutive += 1
        else:
            report.downloaded += 1
            consecutive = 0
        if on_progress:
            on_progress(index, len(todo), track, error)
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            report.stopped = True
            break
        if index < len(todo) and pause > 0:
            sleep(pause)
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
