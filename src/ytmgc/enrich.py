"""Passe modèle : faire juger la bibliothèque, une fois, et garder le résultat.

Le déroulé tient en quatre temps, séparés parce qu'ils n'ont ni la même durée
ni le même prix :

  1. `pending` — quels titres n'ont pas encore de verdict. Le fichier de
     verdicts répond seul, sans réseau ; deux enregistrements du même morceau
     ne comptent que pour un.
  2. `submit` — dépose le lot chez Anthropic et note son identifiant sur le
     disque. À partir de là, l'ordinateur peut s'éteindre : le lot vit chez
     eux et ses résultats restent disponibles vingt-neuf jours.
  3. `collect` — récupère les verdicts et les écrit dans le fichier texte.
  4. `apply` — reporte les verdicts sur les classifications en base, d'où
     partent la planification et l'aperçu.

Séparer 2 et 3 est ce qui rend la passe reprenable : `ytmgc enrich --resume`
ne redépose rien, il va chercher un lot déjà payé.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence

from ytmgc.config import Config
from ytmgc.models import Classification, MatchStatus, Track
from ytmgc.sources.claude import Subject, chunk
from ytmgc.store import Repository
from ytmgc.taxonomy import Taxonomy
from ytmgc.verdicts import Verdict, VerdictBook, load, save, track_key

#: Attente entre deux interrogations d'un lot. Un lot aboutit en général en
#: quelques minutes, au plus en vingt-quatre heures : inutile de harceler.
POLL_SECONDS = 20


class Judge(Protocol):
    """Ce que la passe attend d'un client : de quoi déposer et relire un lot."""

    def submit(self, chunks: Sequence[Sequence[Subject]]) -> str: ...
    def status(self, batch_id: str) -> tuple[str, dict[str, int]]: ...
    def collect(
        self, batch_id: str, chunks: Sequence[Sequence[Subject]]
    ) -> tuple[list[Verdict], list[str]]: ...


@dataclass(frozen=True, slots=True)
class Pending:
    """Un lot déposé, en attente. Écrit sur le disque, pas en base.

    La découpe en requêtes est conservée avec l'identifiant : c'est elle qui
    rattache chaque verdict à son titre, et elle serait perdue au premier
    redémarrage sans cela — les verdicts payés le seraient avec.
    """

    batch_id: str
    model: str
    chunks: list[list[Subject]]
    created_at: str = ""

    @property
    def subjects(self) -> int:
        return sum(len(part) for part in self.chunks)

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "model": self.model,
            "created_at": self.created_at,
            "chunks": [[subject.to_dict() for subject in part] for part in self.chunks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Pending":
        return cls(
            batch_id=data["batch_id"],
            model=data.get("model", ""),
            created_at=data.get("created_at", ""),
            chunks=[
                [Subject.from_dict(item) for item in part] for part in data.get("chunks", [])
            ],
        )


def load_pending(path: str | Path) -> Pending | None:
    file = Path(path)
    if not file.exists():
        return None
    try:
        return Pending.from_dict(json.loads(file.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError):
        # Un fichier illisible ne doit pas bloquer la commande : on repart
        # d'un dépôt neuf plutôt que d'exiger un nettoyage manuel.
        return None


def save_pending(pending: Pending, path: str | Path) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        json.dumps(pending.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
    )


def clear_pending(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


@dataclass(slots=True)
class EnrichStats:
    submitted: int = 0
    collected: int = 0
    applied: int = 0
    problems: list[str] = field(default_factory=list)

    def line(self) -> str:
        parts = [f"{self.collected} verdict(s) récupéré(s)"]
        if self.applied:
            parts.append(f"{self.applied} titre(s) reclassé(s)")
        if self.problems:
            parts.append(f"{len(self.problems)} requête(s) en échec")
        return ", ".join(parts) + "."


def pending_subjects(
    repository: Repository,
    book: VerdictBook,
    *,
    limit: int = 0,
    only_unsorted: bool = False,
) -> list[Subject]:
    """Les titres qui méritent encore d'être jugés.

    `only_unsorted` restreint aux titres que Discogs n'a pas su classer : c'est
    la passe la moins chère, celle qui comble les trous plutôt que de tout
    reprendre.
    """
    classifications = {item.video_id: item for item in repository.classifications()}
    tracks = repository.all_tracks()
    if only_unsorted:
        tracks = [
            track
            for track in tracks
            if classifications.get(track.video_id) is None
            or classifications[track.video_id].status is not MatchStatus.MATCHED
        ]

    missing = book.missing(tracks)
    if limit:
        missing = missing[:limit]
    return [Subject.of(track, classifications.get(track.video_id)) for track in missing]


def submit(
    judge: Judge, subjects: Sequence[Subject], config: Config
) -> Pending:
    """Dépose le lot et note son identifiant avant toute autre chose.

    L'ordre compte : si l'écriture du fichier échouait après le dépôt, on
    aurait payé un lot qu'on ne saurait plus aller chercher.
    """
    parts = chunk(subjects, config.claude.batch_size)
    batch_id = judge.submit(parts)
    pending = Pending(
        batch_id=batch_id,
        model=config.claude.model,
        chunks=parts,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    save_pending(pending, config.claude.pending_file)
    return pending


def wait(
    judge: Judge,
    pending: Pending,
    *,
    on_status: Callable[[str, dict[str, int]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = POLL_SECONDS,
    deadline: float | None = None,
) -> str:
    """Attend la fin du lot, en rendant compte à chaque interrogation.

    Renvoie l'état final. Ne lève pas sur un lot qui traîne : l'appelant décide
    s'il continue d'attendre ou s'il repassera plus tard, le lot étant de toute
    façon récupérable par la suite.
    """
    started = time.monotonic()
    while True:
        state, counts = judge.status(pending.batch_id)
        if on_status is not None:
            on_status(state, counts)
        if state == "ended":
            return state
        if deadline is not None and time.monotonic() - started >= deadline:
            return state
        sleep(poll_seconds)


def collect(judge: Judge, pending: Pending, config: Config) -> tuple[VerdictBook, EnrichStats]:
    """Récupère les verdicts, les fusionne au fichier et l'enregistre.

    Le fichier est écrit avant que le lot ne soit oublié : c'est lui le
    résultat payé, l'identifiant du lot n'est qu'un moyen.
    """
    stats = EnrichStats(submitted=pending.subjects)
    verdicts, problems = judge.collect(pending.batch_id, pending.chunks)
    stats.problems = problems

    book = load(config.claude.verdicts_file)
    for verdict in verdicts:
        book.add(verdict)
    stats.collected = len(verdicts)
    save(book, config.claude.verdicts_file)
    clear_pending(config.claude.pending_file)
    return book, stats


def apply_verdicts(
    repository: Repository,
    book: VerdictBook,
    taxonomy: Taxonomy,
    config: Config,
) -> int:
    """Reporte les verdicts sur les classifications en base.

    Le verdict l'emporte sur Discogs comme sur Last.fm : c'est le seul jugement
    porté sur le morceau lui-même, et c'est celui qu'on a payé. Un verdict peu
    assuré, en revanche, ne détruit pas ce que les bases avaient trouvé — le
    modèle a dit qu'il ne savait pas, on le croit.
    """
    if not len(book):
        return 0

    classifications = {item.video_id: item for item in repository.classifications()}
    applied = 0
    for track in repository.all_tracks():
        verdict = book.get(track_key(track))
        if verdict is None or verdict.confidence < config.claude.min_confidence:
            continue
        current = classifications.get(track.video_id)
        updated = merge(track, current, verdict, taxonomy)
        if updated == current:
            continue
        repository.save_classification(updated)
        applied += 1
    return applied


def merge(
    track: Track,
    current: Classification | None,
    verdict: Verdict,
    taxonomy: Taxonomy,
) -> Classification:
    """La classification telle qu'elle devient une fois le verdict appliqué.

    Le style passe par la table d'alias : le modèle écrit « Hip-Hop » là où le
    projet dit « Hip Hop », et deux graphies donneraient deux playlists.
    """
    style = taxonomy.canonical_style(verdict.style) if verdict.style else None
    styles = (style,) if style else ()
    genres = (verdict.genre,) if verdict.genre else ()

    if current is None:
        return Classification(
            video_id=track.video_id,
            status=MatchStatus.UNMATCHED,
            genres=genres,
            styles=styles,
            mood=verdict.mood or None,
            judged=True,
        )
    return replace(
        current,
        genres=genres or current.genres,
        styles=styles or current.styles,
        mood=verdict.mood or current.mood,
        judged=True,
    )
