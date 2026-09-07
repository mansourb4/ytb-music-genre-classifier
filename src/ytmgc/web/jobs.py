"""Suivi des traitements longs (scan, classification, application).

Le scan d'une bibliothèque et son appariement Discogs prennent des minutes :
l'interface ne peut pas attendre la fin d'une requête HTTP. Chaque traitement
est donc lancé dans un fil d'exécution et interrogé par l'interface.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Job:
    id: str
    kind: str
    #: "en cours", "terminé" ou "échoué".
    status: str = "en cours"
    progress: int = 0
    total: int = 0
    message: str = ""
    error: str | None = None
    result: dict | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "total": self.total,
            "message": self.message,
            "error": self.error,
            "result": self.result,
        }


class JobRunner:
    """Un seul traitement à la fois : les étapes écrivent toutes dans la base."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._current: str | None = None

    def current(self) -> Job | None:
        with self._lock:
            return self._jobs.get(self._current) if self._current else None

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def busy(self) -> bool:
        job = self.current()
        return job is not None and job.status == "en cours"

    def start(self, kind: str, work: Callable[[Job], dict | None]) -> Job:
        """Lance `work` en tâche de fond. Lève RuntimeError si un job tourne déjà."""
        with self._lock:
            running = self._jobs.get(self._current) if self._current else None
            if running is not None and running.status == "en cours":
                raise RuntimeError(f"Un traitement est déjà en cours : {running.kind}")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind)
            self._jobs[job.id] = job
            self._current = job.id

        def run() -> None:
            try:
                job.result = work(job)
                job.status = "terminé"
                job.progress = job.total or job.progress
            except Exception as exc:  # noqa: BLE001 - toute erreur remonte à l'interface
                job.status = "échoué"
                # La trace reste côté serveur ; l'interface n'affiche que le message.
                traceback.print_exc()
                job.error = str(exc) or exc.__class__.__name__

        threading.Thread(target=run, daemon=True, name=f"ytmgc-{kind}").start()
        return job
