"""Apple Music maître : ce que tu changes dans Music est reporté partout.

À chaque `ytmgc synchro`, Apple Music est photographié (titres, favoris,
playlists des dossiers de familles) et comparé à la photo de la synchro
précédente. Les écarts sont tes changements :

- un titre supprimé de la bibliothèque quitte aussi YouTube Music
  (bibliothèque, likes, playlists) et la base, et n'est plus rangé nulle part ;
- un titre passé d'une playlist à une autre y est rangé pour de bon
  (data/placements.txt) ;
- un titre retiré d'une playlist sans être mis ailleurs, ou ajouté à une
  seconde : la synchro te demande ce que tu veux ;
- une playlist créée dans un dossier de famille rejoint le plan
  (config/playlists.toml), une playlist renommée y est renommée, une playlist
  supprimée : la synchro te demande ;
- un favori ajouté ou retiré devient un like ajouté ou retiré.

Ce module ne parle à personne : il compare, et propose. La commande décide,
après t'avoir demandé.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from ytmgc.apple_music import NO_FAMILY

READ_SCRIPT = Path(__file__).with_name("apple_music_read.js")


# ------------------------------------------------------------------ photo

@dataclass(slots=True)
class Photo:
    """Apple Music, en identifiants YouTube."""

    library: set[str] = field(default_factory=set)
    favorites: set[str] = field(default_factory=set)
    #: Identifiant Music de la playlist → (nom dans le plan, titres dans l'ordre).
    playlists: dict[str, tuple[str, list[str]]] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "titres": sorted(self.library),
            "favoris": sorted(self.favorites),
            "playlists": {pid: {"nom": name, "titres": ids}
                          for pid, (name, ids) in self.playlists.items()},
        }

    @classmethod
    def from_json(cls, data: dict) -> "Photo":
        return cls(
            set(data.get("titres") or ()),
            set(data.get("favoris") or ()),
            {pid: (p["nom"], list(p.get("titres") or ()))
             for pid, p in (data.get("playlists") or {}).items()},
        )

    def memberships(self) -> dict[str, set[str]]:
        """Titre → noms des playlists qui le contiennent."""
        found: dict[str, set[str]] = {}
        for name, ids in self.playlists.values():
            for video_id in ids:
                found.setdefault(video_id, set()).add(name)
        return found


def plan_name(folder: str, name: str) -> str:
    """Dossier « Électro » + playlist « Deep techno » → « Électro · Deep techno »."""
    return name if folder == NO_FAMILY else f"{folder} · {name}"


def photo_from_music(
    raw: dict, known: Mapping[str, str], families: Iterable[str],
    tracked: Iterable[str] = (),
) -> Photo:
    """Lecture brute d'Apple Music → photo en identifiants YouTube.

    Les titres qu'Apple Music a d'ailleurs (un CD importé…) n'ont pas
    d'identifiant YouTube : ils sont ignorés. Seules comptent les playlists
    rangées dans un dossier de famille, et celles déjà suivies (`tracked`,
    identifiants Music) : un dossier renommé ne doit pas faire croire que ses
    playlists ont disparu.
    """
    by_pid = {pid: video_id for video_id, pid in known.items()}
    families = set(families)
    tracked = set(tracked)

    def ids(pids: Iterable[str]) -> list[str]:
        return [by_pid[p] for p in pids if p in by_pid]

    playlists = {
        p["pid"]: (plan_name(p["folder"], p["name"]), ids(p.get("tracks") or ()))
        for p in raw.get("playlists") or ()
        if p.get("folder") in families or p.get("pid") in tracked
    }
    return Photo(set(ids(raw.get("tracks") or ())), set(ids(raw.get("favorites") or ())),
                 playlists)


# ------------------------------------------------------------ comparaison

@dataclass(slots=True)
class Question:
    """Un changement qu'on ne tranche pas seul.

    `kind` : « retiré » (sorti de toute playlist), « ajouté » (mis dans une
    playlist de plus), « playlist supprimée ».
    """

    kind: str
    video_id: str | None
    #: Playlists d'avant et d'après (pour un titre), ou le nom (playlist).
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Changes:
    deleted: list[str] = field(default_factory=list)
    favorites_added: list[str] = field(default_factory=list)
    favorites_removed: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    #: (ancien nom, nouveau nom)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    #: Titre → playlist où il est désormais rangé (déplacements sans ambiguïté).
    moves: dict[str, str] = field(default_factory=dict)
    questions: list[Question] = field(default_factory=list)

    def __bool__(self) -> bool:
        return any((self.deleted, self.favorites_added, self.favorites_removed, self.created,
                    self.renamed, self.moves, self.questions))


def compare(before: Photo, after: Photo) -> Changes:
    changes = Changes()
    changes.deleted = sorted(before.library - after.library)
    gone = set(changes.deleted)
    changes.favorites_added = sorted((after.favorites - before.favorites) & after.library)
    changes.favorites_removed = sorted(before.favorites - after.favorites - gone)

    renames: dict[str, str] = {}
    for pid, (name, _) in after.playlists.items():
        if pid not in before.playlists:
            changes.created.append(name)
        elif before.playlists[pid][0] != name:
            renames[before.playlists[pid][0]] = name
            changes.renamed.append((before.playlists[pid][0], name))
    removed_playlists = {name for pid, (name, _) in before.playlists.items()
                         if pid not in after.playlists}
    for name in sorted(removed_playlists):
        changes.questions.append(Question("playlist supprimée", None, [name]))

    old = {v: {renames.get(n, n) for n in names} for v, names in before.memberships().items()}
    new = after.memberships()
    for video_id in sorted(set(old) | set(new)):
        if video_id in gone or video_id not in after.library:
            continue
        was, now = old.get(video_id, set()), new.get(video_id, set())
        added, removed = now - was, was - now
        if not added and not removed:
            continue
        if removed and not added and not now and removed <= removed_playlists:
            continue  # tranché avec la question de la playlist supprimée
        if len(now) == 1 and (removed or not was):
            # Déplacé, ou rangé là pour la première fois : sans ambiguïté.
            changes.moves[video_id] = next(iter(now))
        elif not now:
            changes.questions.append(Question("retiré", video_id, sorted(was), []))
        elif added:
            changes.questions.append(Question("ajouté", video_id, sorted(was), sorted(now)))
        # Retiré d'une de ses deux playlists, il reste dans plusieurs : rien à
        # trancher de plus que ce qu'il reste.
    return changes


# ------------------------------------------------------------ plan (toml)

_BLOCK = re.compile(r"(?m)^\[\[playlist\]\]\s*$")


def _name_line(name: str) -> str:
    return 'nom = "' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def add_playlist(path: Path, name: str, description: str) -> None:
    """Ajoute une playlist manuelle à la fin du plan."""
    text = path.read_text(encoding="utf-8").rstrip("\n")
    desc = description.replace("\\", "\\\\").replace('"', '\\"')
    text += f'\n\n[[playlist]]\n{_name_line(name)}\ndescription = "{desc}"\nmanuelle = true\n'
    path.write_text(text, encoding="utf-8")


def rename_playlist(path: Path, old: str, new: str) -> bool:
    text = path.read_text(encoding="utf-8")
    line = _name_line(old)
    if line not in text:
        return False
    path.write_text(text.replace(line, _name_line(new), 1), encoding="utf-8")
    return True


def remove_playlist(path: Path, name: str) -> bool:
    """Retire le bloc [[playlist]] de ce nom (ses commentaires d'en-tête restent)."""
    text = path.read_text(encoding="utf-8")
    starts = [m.start() for m in _BLOCK.finditer(text)] + [len(text)]
    line = _name_line(name)
    for start, end in zip(starts, starts[1:]):
        block = text[start:end]
        if line in block.splitlines():
            # Le bloc s'arrête à la première ligne vide ou de commentaire qui
            # suit ses clés : ce qui vient après appartient à la suite.
            lines = block.splitlines(keepends=True)
            keep_from = len(lines)
            for i, raw in enumerate(lines[1:], 1):
                stripped = raw.strip()
                if not stripped or stripped.startswith("#"):
                    keep_from = i
                    break
            rest = ("".join(lines[keep_from:]) + text[end:]).lstrip("\n")
            path.write_text(text[:start].rstrip("\n") + "\n\n" + rest, encoding="utf-8")
            return True
    return False


# ------------------------------------------------------------- décisions

NOWHERE = "(aucune)"


@dataclass(slots=True)
class Decisions:
    """Ce que la synchro va écrire, une fois les questions tranchées."""

    #: Titre → playlist (ou « (aucune) ») à inscrire dans data/placements.txt.
    placements: dict[str, str] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    #: Playlists retirées du plan (supprimées dans Music, suppression confirmée).
    removed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    like: list[str] = field(default_factory=list)
    unlike: list[str] = field(default_factory=list)


def decide(
    changes: Changes,
    before: Photo,
    after: Photo,
    answers: Mapping[int, str],
) -> Decisions:
    """Traduit les changements, et les réponses aux questions, en décisions.

    `answers` associe l'indice d'une question à la réponse : pour un titre,
    le nom d'une playlist ou « (aucune) » (absente : rien n'est inscrit, la
    synchro suivante remettra le titre à sa place) ; pour une playlist
    supprimée, « supprimer » ou « recréer ».
    """
    decisions = Decisions(
        placements=dict(changes.moves),
        created=list(changes.created),
        renamed=list(changes.renamed),
        deleted=list(changes.deleted),
        like=list(changes.favorites_added),
        # Un titre supprimé perd aussi son like : inutile de le compter deux fois.
        unlike=sorted(set(changes.favorites_removed) | set(changes.deleted)),
    )
    for video_id in changes.deleted:
        decisions.placements[video_id] = NOWHERE

    now = after.memberships()
    for index, question in enumerate(changes.questions):
        answer = answers.get(index)
        if not answer:
            continue
        if question.kind == "playlist supprimée":
            if answer != "supprimer":
                continue
            name = question.before[0]
            decisions.removed.append(name)
            members = {v for n, ids in before.playlists.values() if n == name for v in ids}
            for video_id in sorted(members):
                if video_id in after.library and not now.get(video_id):
                    decisions.placements[video_id] = NOWHERE
        else:
            decisions.placements[question.video_id] = answer
    return decisions
