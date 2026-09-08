"""Application web locale.

Parcours proposé : connecter son compte, lancer l'analyse, choisir un type de
tri, examiner l'aperçu, puis confirmer. Rien n'est écrit sur le compte tant que
l'utilisateur n'a pas confirmé explicitement.

L'application est destinée à tourner **en local** : elle manipule les
identifiants de session YouTube Music de l'utilisateur, qui ne doivent jamais
transiter par un serveur tiers. Elle écoute donc sur 127.0.0.1 par défaut et
n'expose aucune authentification propre.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ytmgc.classifier import classify_tracks
from ytmgc.config import Config, load_config
from ytmgc.models import Track
from ytmgc.preview import build_preview
from ytmgc.sorting import DEFAULT_SORT_MODE, SORT_MODES, apply_sort_mode
from ytmgc.store import Repository, connect
from ytmgc.sync import apply as apply_actions
from ytmgc.sync import purge
from ytmgc.web.jobs import Job, JobRunner
from ytmgc.web.security import install_token_guard

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class Services:
    """Fabriques injectables : les tests substituent des doubles en mémoire."""

    config: Config
    repository: Repository
    youtube_factory: Callable[[Config], Any]
    discogs_factory: Callable[[Config], Any]
    jobs: JobRunner


def _default_youtube(config: Config):
    from ytmgc.sources.ytmusic import YouTubeMusicClient

    return YouTubeMusicClient(config.youtube.auth_file)


def _default_discogs(config: Config):
    from ytmgc.sources.discogs import DiscogsClient

    return DiscogsClient(config.discogs)


class ConnectRequest(BaseModel):
    #: En-têtes de requête copiés depuis les outils de développement du
    #: navigateur, tels que `ytmusicapi` les attend.
    headers: str = Field(min_length=1)


class PreviewRequest(BaseModel):
    sort_mode: str = DEFAULT_SORT_MODE


class ApplyRequest(BaseModel):
    sort_mode: str = DEFAULT_SORT_MODE
    #: Garde-fou explicite : sans lui, l'application refuse d'écrire.
    confirm: bool = False


class PurgeRequest(BaseModel):
    confirm: bool = False


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="ytmgc", docs_url=None, redoc_url=None)
    config = services.config
    if config.web.token_required():
        if not config.web.access_token:
            raise ValueError(
                "L'interface est exposée hors de la boucle locale : un jeton "
                "d'accès est obligatoire (YTMGC_ACCESS_TOKEN)."
            )
        install_token_guard(app, config.web.access_token)
    repository = services.repository
    jobs = services.jobs

    # ------------------------------------------------------------- lecture

    @app.get("/api/sort-modes")
    def sort_modes() -> dict:
        return {
            "default": DEFAULT_SORT_MODE,
            "modes": [
                {
                    "key": mode.key,
                    "label": mode.label,
                    "summary": mode.summary,
                    "detail": mode.describe(),
                }
                for mode in SORT_MODES
            ],
        }

    @app.get("/api/status")
    def status() -> dict:
        counts = repository.counts_by_status()
        job = jobs.current()
        return {
            "connected": Path(config.youtube.auth_file).exists(),
            "auth_file": config.youtube.auth_file,
            "discogs_token": bool(config.discogs.token),
            "tracks": len(repository.all_tracks()),
            "classified": counts.get("matched", 0),
            "review": counts.get("review", 0),
            "unmatched": counts.get("unmatched", 0),
            "unclassified": len(repository.unclassified_tracks()),
            "managed_playlists": len(repository.managed_playlists()),
            "job": job.to_dict() if job else None,
        }

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Traitement inconnu")
        return job.to_dict()

    # ----------------------------------------------------------- connexion

    @app.post("/api/connect")
    def connect(request: ConnectRequest) -> dict:
        """Écrit le fichier d'authentification à partir des en-têtes collés."""
        from ytmusicapi import setup

        try:
            setup(filepath=config.youtube.auth_file, headers_raw=request.headers)
        except Exception as exc:  # noqa: BLE001 - message d'erreur remonté tel quel
            raise HTTPException(400, f"En-têtes invalides : {exc}") from exc

        try:
            services.youtube_factory(config)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Connexion refusée par YouTube Music : {exc}") from exc
        return {"connected": True}

    # ------------------------------------------------------------ analyse

    @app.post("/api/analyse")
    def analyse() -> dict:
        """Scanne la bibliothèque puis l'apparie à Discogs, en une seule passe."""
        if jobs.busy():
            raise HTTPException(409, "Un traitement est déjà en cours")

        def work(job: Job) -> dict:
            job.message = "Lecture de la bibliothèque YouTube Music…"
            tracks: list[Track] = services.youtube_factory(config).scan(config.youtube.sources)
            repository.upsert_tracks(tracks)

            pending = repository.unclassified_tracks()
            job.total = len(pending)
            job.message = f"{len(tracks)} titres lus. Interrogation de Discogs…"

            def progress(track, _classification) -> None:
                job.progress += 1
                job.message = f"Discogs : {job.progress}/{job.total} — {track.label()}"

            stats = classify_tracks(
                pending, repository, services.discogs_factory(config), config, progress=progress
            )
            job.message = stats.line()
            return {"scanned": len(tracks), "summary": stats.line()}

        return jobs.start("analyse", work).to_dict()

    # ------------------------------------------------------------- aperçu

    @app.post("/api/preview")
    def preview(request: PreviewRequest) -> dict:
        scoped = _scoped(request.sort_mode)
        remote = _remote_playlists()
        summary, _, actions = build_preview(
            repository, scoped, sort_mode=request.sort_mode, remote=remote
        )
        return {
            "preview": summary.to_dict(),
            "actions": [action.summary() for action in actions],
            "remote_known": remote is not None,
        }

    # -------------------------------------------------------- application

    @app.post("/api/apply")
    def apply_changes(request: ApplyRequest) -> dict:
        if not request.confirm:
            raise HTTPException(400, "Confirmation requise avant toute écriture")
        if jobs.busy():
            raise HTTPException(409, "Un traitement est déjà en cours")

        scoped = _scoped(request.sort_mode)

        def work(job: Job) -> dict:
            client = services.youtube_factory(config)
            job.message = "Lecture des playlists existantes…"
            _, plans, actions = build_preview(
                repository, scoped, sort_mode=request.sort_mode,
                remote=client.list_playlists(),
            )
            job.total = len(actions)
            if not actions:
                job.message = "Le compte est déjà à jour."
                return {"applied": 0}

            job.message = f"Application de {len(actions)} action(s)…"
            log = apply_actions(
                actions, plans, client, scoped,
                dry_run=False, on_created=repository.remember_playlist,
            )
            job.progress = len(actions)
            job.message = f"{len(actions)} action(s) appliquée(s)."
            return {"applied": len(actions), "log": log}

        return jobs.start("application", work).to_dict()

    # ----------------------------------------------------------- annulation

    @app.post("/api/purge")
    def purge_playlists(request: PurgeRequest) -> dict:
        """Supprime les playlists générées. Irréversible, d'où la confirmation."""
        if not request.confirm:
            raise HTTPException(400, "Confirmation requise avant toute suppression")
        if jobs.busy():
            raise HTTPException(409, "Un traitement est déjà en cours")

        def work(job: Job) -> dict:
            client = services.youtube_factory(config)
            job.message = "Recherche des playlists générées…"
            log = purge(
                client.list_playlists(), client, config,
                dry_run=False, on_deleted=repository.forget_playlist,
            )
            job.total = job.progress = len(log)
            job.message = f"{len(log)} playlist(s) supprimée(s)."
            return {"deleted": len(log), "log": log}

        return jobs.start("suppression", work).to_dict()

    # ------------------------------------------------------------- outils

    def _scoped(sort_mode: str) -> Config:
        try:
            return apply_sort_mode(config, sort_mode)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def _remote_playlists():
        """État distant, ou None si le compte n'est pas encore joignable.

        L'aperçu doit rester consultable hors connexion : on présente alors
        tout comme une création, ce qui est exact pour une première utilisation.
        """
        try:
            return services.youtube_factory(config).list_playlists()
        except Exception:  # noqa: BLE001 - absence d'authentification, réseau, quota
            return None

    # -------------------------------------------------------------- pages

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def build_default_app(config_path: Path | None = None, config: Config | None = None) -> FastAPI:
    config = config if config is not None else load_config(config_path)
    return create_app(
        Services(
            config=config,
            repository=Repository(connect(config.store.path)),
            youtube_factory=_default_youtube,
            discogs_factory=_default_discogs,
            jobs=JobRunner(),
        )
    )
