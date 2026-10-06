"""Relecture par playlist : Claude.ai cherche les intrus, on tranche.

Vérifier les titres un par un ne passe pas à l'échelle : huit cents titres
« à vérifier », c'est des heures, et la moitié des clics confirme un titre
déjà bien rangé. Surtout, un titre jugé seul ne dit rien de la question qui
compte — garde-t-il la vibe de sa playlist ? Un intrus se voit à côté des
autres.

On exporte donc les *playlists*, pas les titres : chacune avec son nom, sa
description et tous ses titres, en quelques fichiers à glisser dans Claude.ai
(comme les verdicts : rien à payer en plus de l'abonnement). Claude ne répond
que pour les intrus, une ligne par titre :

    artiste | titre | playlist proposée | raison

Ses réponses deviennent des *propositions*, tenues dans `data/propositions.txt`
tant qu'on ne les a pas tranchées. Accepter en fait un déplacement ; refuser
valide le verdict du titre, qui ne sera plus proposé. Rien ne bouge sans un
clic : une proposition n'est qu'un avis.

Ce qui a déjà été décidé à la main — titre déplacé, verdict validé — figure
dans les fichiers pour situer la playlist, marqué « [validé] », et n'est
jamais proposé.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ytmgc.chat import ChatError, _find, _lines
from ytmgc.matching.normalize import fold, slugify
from ytmgc.models import Track
from ytmgc.placements import NOWHERE
from ytmgc.verdicts import entry_key, main_artist, track_key

#: « playlists-3-sur-7.txt » dit à lui seul où l'on en est.
FILE_RE = re.compile(r"^playlists-\d+-sur-\d+\.txt$")
MANIFEST = "relecture.json"
SUBDIR = "relecture"

#: Titres par fichier. Claude ne répond que pour les intrus : la réponse est
#: courte, et c'est la liste qu'il doit tenir en tête pour juger une playlist.
FILE_SIZE = 600

FIXED = "[validé]"
NOTHING = "RIEN"

_PART_RE = re.compile(r"^\W*fichier\s+(\d+)\s+sur\s+(\d+)", re.I)

HEADER = """\
# Propositions de la relecture par playlist — en attente de ta décision.
#
# Une ligne par titre, colonnes séparées par des tabulations :
#   artiste  |  titre  |  playlist proposée  |  raison
#
# Écrites par l'import d'une réponse de Claude.ai, retirées quand tu acceptes
# (le titre est déplacé) ou refuses (il reste où il est). « (aucune) » propose
# de ne ranger le titre nulle part.
"""


# ------------------------------------------------------------ propositions


@dataclass(frozen=True, slots=True)
class Proposal:
    artist: str
    title: str
    playlist: str
    reason: str = ""

    @property
    def key(self) -> str:
        return entry_key(self.artist, self.title)

    @property
    def target(self) -> str | None:
        """Clé de la playlist proposée ; None pour « (aucune) »."""
        return None if self.playlist == NOWHERE else "plan/" + slugify(self.playlist)

    def line(self) -> str:
        return "\t".join(
            text.replace("\t", " ").replace("\n", " ")
            for text in (self.artist, self.title, self.playlist, self.reason)
        )


class ProposalBook:
    def __init__(self, proposals: list[Proposal] | None = None) -> None:
        self._by_key: dict[str, Proposal] = {}
        for proposal in proposals or []:
            self._by_key[proposal.key] = proposal

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self):
        return iter(self._by_key.values())

    def get(self, key: str) -> Proposal | None:
        return self._by_key.get(key)

    def add(self, proposal: Proposal) -> None:
        self._by_key[proposal.key] = proposal

    def discard(self, key: str) -> bool:
        return self._by_key.pop(key, None) is not None

    def lines(self) -> list[str]:
        ordered = sorted(self._by_key.values(),
                         key=lambda p: (p.artist.casefold(), p.title.casefold()))
        return [proposal.line() for proposal in ordered]


def _parse_line(line: str) -> Proposal | None:
    stripped = line.strip("\n")
    if not stripped.strip() or stripped.lstrip().startswith("#"):
        return None
    parts = [part.strip() for part in stripped.split("\t")]
    if len(parts) < 3 or not all(parts[:3]):
        return None
    return Proposal(parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else "")


def load(path: str | Path) -> ProposalBook:
    file = Path(path)
    if not file.exists():
        return ProposalBook()
    lines = file.read_text(encoding="utf-8").splitlines()
    return ProposalBook([p for p in (_parse_line(line) for line in lines) if p is not None])


def save(book: ProposalBook, path: str | Path) -> int:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(HEADER + "\n".join(book.lines()) + "\n", encoding="utf-8")
    temporary.replace(file)
    return len(book)


def discard(path: str | Path, tracks: list[Track]) -> int:
    """Retire les propositions de ces titres : on vient d'en décider autrement."""
    book = load(path)
    removed = sum(1 for track in tracks if book.discard(track_key(track)))
    if removed:
        save(book, path)
    return removed


# ------------------------------------------------------------------ export


def _instructions() -> str:
    return f"""\
Tu es musicologue et tu relis les playlists de la discothèque de quelqu'un.

Ces playlists ont été faites par des règles, à partir d'un genre, d'un style
et d'une ambiance attribués à chaque titre. Ce que la personne veut : quand
elle lance une playlist, ne pas être surprise par un changement de musique —
la même vibe du premier au dernier titre. Les règles se trompent parfois ; à
toi de trouver les intrus.

Pour chaque playlist de ce fichier :

- Lis-la en entier, et fais-toi une idée de ce qu'elle est à l'écoute.
- Repère les titres qui détonnent : une autre énergie, une autre époque, un
  autre univers sonore, ou qui seraient clairement mieux ailleurs.
- Pour chacun, choisis dans PLAYLISTS DISPONIBLES celle où il irait le mieux.
  Écris « {NOWHERE} » s'il n'a sa place dans aucune (un sketch, un jingle).

Règles :

- Ne propose que ce dont tu es convaincu. Dans le doute, laisse le titre où il
  est : chaque proposition sera relue à la main, une par une.
- Juge le morceau tel qu'il sonne, pas l'étiquette de l'artiste.
- Les titres marqués {FIXED} ont été placés à la main : ne les propose jamais,
  ils sont là pour situer la playlist.
- Le nom de la playlist proposée doit être recopié exactement depuis la liste.

FORMAT DE LA RÉPONSE

Un bloc de code. Première ligne : la ligne « FICHIER … SUR … » de l'en-tête de
ce fichier, recopiée. Puis une ligne par intrus :

    artiste | titre | playlist proposée | raison en quelques mots

artiste et titre recopiés exactement depuis la liste. N'écris jamais « | »
dans un champ. S'il n'y a aucun intrus dans tout le fichier, écris seulement
{NOTHING} sous la ligne FICHIER. Pas de phrase avant ni après le bloc.
"""


def _menu(plan) -> str:
    lines = ["PLAYLISTS DISPONIBLES (nom exact — ce qu'on y entend)", ""]
    for playlist in sorted(plan.playlists, key=lambda p: p.name.casefold()):
        about = playlist.description or ""
        lines.append(f"- {playlist.name}" + (f" — {about}" if about else ""))
    return "\n".join(lines)


def _track_line(track, fixed: bool) -> str:
    hints = " · ".join([*track.styles[:2], *([track.mood] if track.mood else [])])
    fields = [track.main_artist or track.artist, track.title, hints]
    line = " | ".join(text.replace("|", "/") for text in fields)
    return f"{line} {FIXED}" if fixed else line


def _playlist_block(playlist, about: str, fixed_keys: set[str]) -> str:
    lines = [f"### {playlist.name} — {len(playlist.tracks)} titres"]
    if about:
        lines.append(about)
    lines.append("")
    for track in playlist.tracks:
        fixed = track.moved or entry_key(track.main_artist, track.title) in fixed_keys
        lines.append(_track_line(track, fixed))
    return "\n".join(lines)


def _family(name: str) -> str:
    return name.split(" · ", 1)[0] if " · " in name else name


def pack(playlists: list, size: int = FILE_SIZE) -> list[list]:
    """Les playlists réparties en fichiers d'au plus `size` titres, sans jamais
    en couper une, et les familles ensemble : Claude juge mieux une playlist
    quand ses voisines sont sous ses yeux."""
    ordered = sorted(playlists, key=lambda p: (_family(p.name).casefold(), p.name.casefold()))
    files: list[list] = []
    current: list = []
    count = 0
    for playlist in ordered:
        if current and count + len(playlist.tracks) > size:
            files.append(current)
            current, count = [], 0
        current.append(playlist)
        count += len(playlist.tracks)
    if current:
        files.append(current)
    return files


def build_file(playlists: list, part: int, parts: int, plan, fixed_keys: set[str]) -> str:
    tracks = sum(len(p.tracks) for p in playlists)
    written = plan.by_key()
    blocks = [
        _playlist_block(p, written[p.key].description if p.key in written else "", fixed_keys)
        for p in playlists
    ]
    return "\n".join((
        f"FICHIER {part} SUR {parts} — {len(playlists)} playlists, {tracks} titres à relire",
        "",
        _instructions(),
        _menu(plan),
        "",
        "PLAYLISTS À RELIRE (artiste | titre | style · ambiance)",
        "",
        "\n\n".join(blocks),
        "",
    ))


def directory(config) -> Path:
    return Path(config.claude.export_dir) / SUBDIR


@dataclass(frozen=True, slots=True)
class ExportFile:
    name: str
    playlists: int
    tracks: int


def export_review(summary, plan, config, fixed_keys: set[str]) -> list[ExportFile]:
    """Écrit les playlists de l'aperçu en fichiers pour Claude.ai, d'un coup.

    Les fichiers d'une relecture précédente sont remplacés : les playlists ont
    bougé depuis.
    """
    folder = directory(config)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        if FILE_RE.match(old.name) or old.name == MANIFEST:
            old.unlink()

    groups = pack([p for p in summary.playlists if p.tracks])
    files: list[ExportFile] = []
    for number, group in enumerate(groups, start=1):
        name = f"playlists-{number}-sur-{len(groups)}.txt"
        (folder / name).write_text(
            build_file(group, number, len(groups), plan, fixed_keys), encoding="utf-8"
        )
        files.append(ExportFile(name, len(group), sum(len(p.tracks) for p in group)))
    _write_manifest(config, {"files": [
        {"name": f.name, "playlists": f.playlists, "tracks": f.tracks} for f in files
    ], "answered": []})
    return files


def _read_manifest(config) -> dict:
    path = directory(config) / MANIFEST
    if not path.exists():
        return {"files": [], "answered": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"files": [], "answered": []}


def _write_manifest(config, data: dict) -> None:
    folder = directory(config)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / MANIFEST).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                   encoding="utf-8")


def export_state(config) -> list[dict]:
    """Les fichiers de la dernière relecture, et ceux dont la réponse est revenue."""
    data = _read_manifest(config)
    answered = set(data.get("answered", []))
    files = [item for item in data.get("files", []) if FILE_RE.match(str(item.get("name", "")))]
    return [{**item, "answered": number in answered}
            for number, item in enumerate(files, start=1)]


def export_path(config, name: str) -> Path | None:
    """Chemin d'un fichier exporté ; le nom vient d'une URL et est validé."""
    if not FILE_RE.match(name):
        return None
    path = directory(config) / name
    return path if path.is_file() else None


# ---------------------------------------------------------------- lecture


@dataclass(slots=True)
class Reading:
    proposals: list[Proposal] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    #: Titres que Claude propose de laisser où ils sont déjà.
    in_place: int = 0
    #: Numéro du fichier auquel la réponse dit répondre.
    part: int | None = None


def _playlist_index(plan) -> tuple[dict[str, str], dict[str, str]]:
    """Noms tolérés -> nom exact : le nom complet sans accents ni casse, et le
    nom sans sa famille quand il n'est porté que par une playlist."""
    full = {fold(p.name): p.name for p in plan.playlists}
    short: dict[str, list[str]] = {}
    for p in plan.playlists:
        if " · " in p.name:
            short.setdefault(fold(p.name.split(" · ", 1)[1]), []).append(p.name)
    return full, {k: v[0] for k, v in short.items() if len(v) == 1}


def _target(name: str, full: dict[str, str], short: dict[str, str]) -> str | None:
    folded = fold(name.strip().strip("«»\"'"))
    if folded in (fold(NOWHERE), "aucune"):
        return NOWHERE
    return full.get(folded) or short.get(folded)


def read_answer(
    text: str, tracks: list[Track], plan, located: dict[str, str | None], fixed: set[str]
) -> Reading:
    """Lit une réponse collée. `located` : clé de morceau -> clé de la playlist
    où il se trouve (None s'il n'est nulle part) ; `fixed` : morceaux décidés à
    la main, jamais proposés."""
    lines = _lines(text)
    if not lines:
        raise ChatError("Rien à lire : colle la réponse de Claude.")
    index = {track_key(track): track for track in tracks}
    full, short = _playlist_index(plan)
    reading = Reading()
    readable = 0
    seen: set[str] = set()

    for line in lines:
        part = _PART_RE.match(line)
        if part:
            reading.part = int(part.group(1))
            readable += 1
            continue
        if fold(line.strip("`* ")) == fold(NOTHING):
            readable += 1
            continue
        if line.startswith("```"):
            continue
        fields = [f.strip() for f in (line.split("\t") if "\t" in line else line.strip("|").split("|"))]
        if len(fields) < 3 or fold(fields[0]) == "artiste":
            continue
        readable += 1
        artist, title, wanted = fields[:3]
        label = f"{artist} – {title}"
        track = _find(index, artist, title)
        if track is None:
            reading.rejected.append(f"{label} : aucun morceau de ce nom dans la bibliothèque")
            continue
        key = track_key(track)
        if key in seen:
            continue
        seen.add(key)
        name = _target(wanted, full, short)
        if name is None:
            reading.rejected.append(f"{label} : playlist « {wanted} » absente du plan")
            continue
        if key in fixed:
            reading.rejected.append(f"{label} : déjà placé à la main, laissé tel quel")
            continue
        proposal = Proposal(main_artist(track), track.title, name, " | ".join(fields[3:]).strip())
        if located.get(key) == proposal.target:
            reading.in_place += 1
            continue
        reading.proposals.append(proposal)

    if not readable:
        raise ChatError(
            "Aucune ligne au format « artiste | titre | playlist proposée | raison » "
            "dans ce texte. Copie la réponse avec le bouton « Copier » du bloc de code."
        )
    return reading


def import_answer(text: str, tracks: list[Track], plan, located, fixed, config) -> Reading:
    """Lit une réponse et range ses propositions avec celles en attente."""
    reading = read_answer(text, tracks, plan, located, fixed)
    path = config.taxonomy.proposals_file
    book = load(path)
    for proposal in reading.proposals:
        book.add(proposal)
    save(book, path)
    if reading.part is not None:
        data = _read_manifest(config)
        answered = set(data.get("answered", []))
        answered.add(reading.part)
        data["answered"] = sorted(answered)
        _write_manifest(config, data)
    return reading


# ------------------------------------------------------------------ aperçu


def pending(path, tracks: dict[str, Track], located: dict[str, str | None],
            fixed: set[str], plan) -> list[tuple[Track, Proposal]]:
    """Les propositions encore à trancher, rattachées à un titre de la
    bibliothèque. Une proposition dont le titre a été décidé à la main entre-
    temps, déjà à sa place, ou vers une playlist sortie du plan, est tue."""
    book = load(path)
    if not len(book):
        return []
    keys = set(plan.by_key())
    by_song: dict[str, Track] = {}
    for track in tracks.values():
        by_song.setdefault(track_key(track), track)
    result = []
    for proposal in book:
        track = by_song.get(proposal.key)
        if track is None or proposal.key in fixed:
            continue
        target = proposal.target
        if target is not None and target not in keys:
            continue
        if located.get(proposal.key) == target:
            continue
        result.append((track, proposal))
    return result


def fixed_keys(verdict_book, placement_book) -> set[str]:
    """Morceaux décidés à la main : déplacés, ou dont le verdict est validé."""
    from ytmgc.verdicts import MANUAL

    return ({p.key for p in placement_book}
            | {v.key for v in verdict_book if v.source == MANUAL})


def located(plans, tracks: dict[str, Track]) -> dict[str, str | None]:
    """Clé de morceau -> clé de la playlist du plan où il se trouve."""
    where: dict[str, str | None] = {track_key(t): None for t in tracks.values()}
    for item in plans:
        for video_id in item.video_ids:
            track = tracks.get(video_id)
            if track is not None:
                where[track_key(track)] = item.key
    return where
