"""Apple Music : un dossier par famille, une playlist par nom, chaque titre rangé.

Les fichiers restent où `ytmgc telecharge` les a mis (~/Music/Playlists) :
Apple Music les référence sans les copier. Le travail côté Apple Music est fait
par `apple_music.js`, lancé par osascript ; ce module prépare ce qu'il doit
faire et retient, d'une fois sur l'autre, quel titre d'Apple Music correspond
à quel titre YouTube (`.apple-music.json` dans le dossier racine).

Relancer est sans risque : un titre déjà là n'est pas ajouté deux fois, un
fichier déplacé est retrouvé, une playlist n'est remplie que de ce qui manque.
Seules les playlists rangées dans les dossiers des familles sont touchées ;
rien n'est jamais supprimé de la bibliothèque.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ytmgc.download import Wanted
from ytmgc.models import PlaylistPlan, Track

SCRIPT = Path(__file__).with_name("apple_music.js")
STATE_FILE = ".apple-music.json"
#: Playlist sans « Famille · » dans son nom : rangée dans ce dossier.
NO_FAMILY = "Divers"


def split_name(playlist_name: str) -> tuple[str, str]:
    """« Électro · Deep techno » → (« Électro », « Deep techno »)."""
    family, sep, name = playlist_name.partition(" · ")
    return (family, name) if sep else (NO_FAMILY, playlist_name)


def build_input(
    root: Path,
    plans: Iterable[PlaylistPlan],
    wanted: Mapping[str, Wanted],
    present: Mapping[str, Path],
    state: "State | None" = None,
    *,
    dry_run: bool = False,
) -> dict:
    """Ce qu'Apple Music doit contenir, pour les titres dont le fichier est là."""
    state = state or State()
    items = [
        {
            "id": video_id,
            "path": str(root / present[video_id]),
            "genre": item.tags.genre,
            "grouping": item.tags.grouping,
        }
        for video_id, item in wanted.items()
        if video_id in present
    ]
    available = {item["id"] for item in items}
    playlists = []
    for plan in plans:
        family, name = split_name(plan.name)
        ids = [v for v in plan.video_ids if v in available]
        if ids:
            playlists.append({"family": family, "name": name, "ids": ids})
    return {
        "root": str(root),
        "known": dict(state.known),
        "items": items,
        "playlists": playlists,
        "families": sorted(state.families | {p["family"] for p in playlists}),
        "dryRun": dry_run,
    }


@dataclass(slots=True)
class State:
    """Ce qui est retenu d'un lancement à l'autre."""

    #: Identifiant YouTube → identifiant du titre dans Apple Music.
    known: dict[str, str] = field(default_factory=dict)
    #: Dossiers de familles déjà gérés par l'outil.
    families: set[str] = field(default_factory=set)


def load_state(root: Path) -> State:
    try:
        data = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return State()
    return State(dict(data.get("titres") or {}), set(data.get("familles") or ()))


def save_state(root: Path, state: State) -> None:
    data = {"familles": sorted(state.families), "titres": state.known}
    (root / STATE_FILE).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                   encoding="utf-8")


@dataclass(slots=True)
class Result:
    known: dict[str, str] = field(default_factory=dict)
    added: int = 0
    relocated: int = 0
    retagged: int = 0
    missing: list[str] = field(default_factory=list)
    created_folders: list[str] = field(default_factory=list)
    created_playlists: list[str] = field(default_factory=list)
    track_adds: int = 0
    track_removals: int = 0
    stale: list[str] = field(default_factory=list)
    #: Apple Music copie les fichiers ajoutés dans son propre dossier.
    copied: bool = False
    copied_to: str | None = None

    @classmethod
    def from_json(cls, data: dict) -> "Result":
        return cls(
            known=dict(data.get("known") or {}),
            added=data.get("added", 0),
            relocated=data.get("relocated", 0),
            retagged=data.get("retagged", 0),
            missing=list(data.get("missing") or []),
            created_folders=list(data.get("createdFolders") or []),
            created_playlists=list(data.get("createdPlaylists") or []),
            track_adds=data.get("trackAdds", 0),
            track_removals=data.get("trackRemovals", 0),
            stale=list(data.get("stale") or []),
            copied=bool(data.get("copied")),
            copied_to=data.get("copiedTo"),
        )


def _osascript(
    script: Path,
    payload: dict,
    *,
    on_line: Callable[[str], None],
    popen: Callable[..., subprocess.Popen],
) -> dict:
    """Lance un script JXA sur `payload`, relaie son avancement, rend sa sortie."""
    if sys.platform != "darwin" and popen is subprocess.Popen:
        raise RuntimeError("Apple Music ne se pilote que depuis un Mac.")
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8",
                                     delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        input_path = handle.name
    # La sortie (un JSON de quelques centaines de Ko) part dans un fichier :
    # dans un tube, elle bloquerait le script pendant qu'on lit l'avancement.
    output_path = input_path + ".out"
    try:
        with open(output_path, "w", encoding="utf-8") as output_file:
            process = popen(
                ["osascript", "-l", "JavaScript", str(script), input_path],
                stdout=output_file, stderr=subprocess.PIPE, text=True,
            )
            errors: list[str] = []
            for line in process.stderr:
                line = line.rstrip("\n")
                errors.append(line)
                on_line(line)
            code = process.wait()
        if code != 0:
            detail = errors[-1] if errors else f"code de sortie {code}"
            raise RuntimeError(f"Le script Apple Music a échoué : {detail}")
        return json.loads(Path(output_path).read_text(encoding="utf-8"))
    finally:
        Path(input_path).unlink(missing_ok=True)
        Path(output_path).unlink(missing_ok=True)


def run_script(
    payload: dict,
    *,
    on_line: Callable[[str], None] = print,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> Result:
    """Lance le script Apple Music et relaie son avancement ligne à ligne."""
    return Result.from_json(_osascript(SCRIPT, payload, on_line=on_line, popen=popen))


# ----------------------------------------------------------------- favoris

FAVORITES_SCRIPT = Path(__file__).with_name("apple_music_favorites.js")


def favorite_ids(
    liked: Iterable[dict],
    tracks: Mapping[str, "Track"],
    known: Mapping[str, str],
) -> tuple[list[str], list[str]]:
    """Titres d'Apple Music à mettre en favoris, et likés introuvables dans Music.

    Un titre liké sur YouTube correspond à un titre d'Apple Music par son
    identifiant, ou à défaut par le morceau (même artiste, même titre) : c'est
    le cas des doublons, téléchargés sous l'autre version.
    """
    from ytmgc.download import song_key

    by_song: dict[str, str] = {}
    for video_id, pid in known.items():
        track = tracks.get(video_id)
        if track is not None:
            by_song.setdefault(song_key(track), pid)
    pids: list[str] = []
    absent: list[str] = []
    for entry in liked:
        video_id = entry.get("video_id")
        if not video_id:
            continue
        pid = known.get(video_id)
        if pid is None:
            track = tracks.get(video_id) or Track(
                video_id, entry.get("title") or "", tuple(entry.get("artists") or ()))
            pid = by_song.get(song_key(track))
        if pid is None:
            absent.append(entry.get("label") or video_id)
        elif pid not in pids:
            pids.append(pid)
    return pids, absent


@dataclass(slots=True)
class FavoritesResult:
    favorited: int = 0
    already: int = 0
    #: Identifiants retenus qui ne sont plus dans la bibliothèque Music.
    gone: int = 0


def run_favorites(
    pids: list[str],
    *,
    dry_run: bool = False,
    on_line: Callable[[str], None] = print,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> FavoritesResult:
    data = _osascript(FAVORITES_SCRIPT, {"pids": pids, "dryRun": dry_run},
                      on_line=on_line, popen=popen)
    return FavoritesResult(data.get("favorited", 0), data.get("already", 0), data.get("gone", 0))
