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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ytmgc import verdicts as verdict_file
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
    #: Source de tags par titre. Peut renvoyer None : la fonctionnalité est
    #: facultative, et son absence ne doit rien empêcher.
    lastfm_factory: Callable[[Config], Any] = lambda _config: None
    #: Client du modèle. Peut renvoyer None : la passe est facultative et
    #: payante, et son absence ne doit rien empêcher du reste.
    judge_factory: Callable[[Config], Any] = lambda _config: None
    jobs: JobRunner = field(default_factory=JobRunner)
    #: Décomptes déjà mesurés, par source. Ils ne varient guère au fil d'une
    #: session, et les remesurer coûterait un appel par affichage.
    counts: dict[str, int | None] = field(default_factory=dict)


def _default_youtube(config: Config):
    from ytmgc.sources.oauth import load_client
    from ytmgc.sources.ytmusic import YouTubeMusicClient

    return YouTubeMusicClient(
        config.youtube.auth_file,
        oauth_client=load_client(config.youtube.oauth_client_file),
    )


def _default_discogs(config: Config):
    from ytmgc.sources.discogs import DiscogsClient

    return DiscogsClient(config.discogs)


def _default_lastfm(config: Config):
    """Client de tags, ou None : sans clé, l'analyse s'en passe."""
    if not (config.lastfm.enabled and config.lastfm.api_key):
        return None
    from ytmgc.sources.lastfm import LastfmClient

    return LastfmClient(config.lastfm)


def _default_judge(config: Config):
    """Client du modèle, ou None : sans clé, l'outil fonctionne comme avant."""
    if not (config.claude.enabled and config.claude.api_key):
        return None
    from ytmgc.sources.claude import ClaudeClient
    from ytmgc.taxonomy import load_taxonomy

    return ClaudeClient(config.claude, load_taxonomy())


class ConnectRequest(BaseModel):
    #: En-têtes de requête copiés depuis les outils de développement du
    #: navigateur, tels que `ytmusicapi` les attend.
    headers: str = Field(min_length=1)


class BrowserImportRequest(BaseModel):
    #: None = chercher dans tous les navigateurs installés.
    browser: str | None = None


class OAuthStartRequest(BaseModel):
    """Identifiant client OAuth créé par l'utilisateur dans Google Cloud."""

    client_id: str = Field(min_length=10)
    client_secret: str = Field(min_length=5)


class OAuthPollRequest(BaseModel):
    device_code: str = Field(min_length=1)


class AnalyseRequest(BaseModel):
    #: Sources à scanner. Vide = celles de la configuration.
    sources: list[str] | None = None


class PreviewRequest(BaseModel):
    sort_mode: str = DEFAULT_SORT_MODE


class ApplyRequest(BaseModel):
    sort_mode: str = DEFAULT_SORT_MODE
    #: Garde-fou explicite : sans lui, l'application refuse d'écrire.
    confirm: bool = False
    #: Playlists décochées dans l'aperçu : ni créées, ni modifiées, ni vidées.
    excluded_playlists: list[str] = Field(default_factory=list)
    #: Titres décochés, par clé de playlist.
    excluded_tracks: dict[str, list[str]] = Field(default_factory=dict)


class EnrichRequest(BaseModel):
    #: Ne juger que les titres que Discogs n'a pas su classer.
    only_unsorted: bool = False
    limit: int = 0
    #: Garde-fou explicite : cette passe est la seule à coûter de l'argent.
    confirm: bool = False


class LookupRequest(BaseModel):
    artist: str = Field(min_length=1)
    title: str = Field(min_length=1)
    album: str | None = None
    #: Redemander un titre déjà jugé (et le repayer).
    force: bool = False


class PurgeRequest(BaseModel):
    confirm: bool = False
    #: Playlists à supprimer. Absent = toutes celles portant le marqueur.
    playlist_ids: list[str] | None = None


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
            "lastfm_key": bool(config.lastfm.enabled and config.lastfm.api_key),
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
        """Écrit le fichier d'authentification à partir d'un collage.

        Accepte une commande « Copier comme cURL » aussi bien qu'une liste
        d'en-têtes bruts : seul le cookie de session est extrait, le reste des
        en-têtes étant reconstruit. N'importe quelle requête authentifiée du
        domaine convient donc, sans avoir à en trouver une précise.
        """
        from ytmgc.sources.browser_session import BrowserSessionError, auth_file_from_paste

        try:
            auth_file_from_paste(request.headers, config.youtube.auth_file)
        except BrowserSessionError as exc:
            raise HTTPException(400, str(exc)) from exc

        try:
            services.youtube_factory(config)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Connexion refusée par YouTube Music : {exc}") from exc
        return {"connected": True}

    @app.get("/api/connect/methods")
    def connect_methods() -> dict:
        """Méthodes de connexion disponibles, et laquelle est déjà configurée."""
        from ytmgc.sources.browser_session import BROWSERS
        from ytmgc.sources.oauth import load_client

        client = load_client(config.youtube.oauth_client_file)
        return {
            "oauth_client_configured": client is not None,
            "auth_file": config.youtube.auth_file,
            "connected": Path(config.youtube.auth_file).exists(),
            "browsers": list(BROWSERS),
        }

    @app.post("/api/connect/browser-session")
    def connect_browser_session(request: BrowserImportRequest) -> dict:
        """Reprend la session d'un navigateur où l'utilisateur est déjà connecté.

        Voie la plus directe : aucune saisie, aucun formulaire. Elle échoue
        proprement quand aucune session n'est trouvée, l'interface renvoyant
        alors vers les autres méthodes.
        """
        from ytmgc.sources.browser_session import BrowserSessionError, import_session

        try:
            source = import_session(config.youtube.auth_file, request.browser)
        except BrowserSessionError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"connected": True, "source": source}

    @app.post("/api/connect/browser-login")
    def connect_browser_login() -> dict:
        """Ouvre une fenêtre de connexion et attend que l'utilisateur s'identifie.

        L'attente peut durer plusieurs minutes : c'est donc un traitement de
        fond, suivi comme les autres depuis l'interface.
        """
        if jobs.busy():
            raise HTTPException(409, "Un traitement est déjà en cours")

        def work(job: Job) -> dict:
            from ytmgc.sources.browser_login import DEFAULT_TIMEOUT_S, login_and_capture

            job.total = DEFAULT_TIMEOUT_S
            job.message = "Fenêtre ouverte : connecte-toi à YouTube Music."

            def on_wait(remaining: int) -> None:
                job.progress = DEFAULT_TIMEOUT_S - remaining
                job.message = f"En attente de ta connexion… ({remaining} s restantes)"

            source = login_and_capture(config.youtube.auth_file, on_wait=on_wait)
            job.message = "Compte connecté."
            return {"connected": True, "source": source}

        return jobs.start("connexion", work).to_dict()

    @app.post("/api/connect/oauth/start")
    def oauth_start(request: OAuthStartRequest) -> dict:
        """Première étape : obtenir de Google un code à saisir.

        C'est la voie utilisable depuis un téléphone seul : aucun outil de
        développement, juste une URL et un code.
        """
        from ytmgc.sources.oauth import OAuthClient, save_client, start_device_flow

        client = OAuthClient(request.client_id.strip(), request.client_secret.strip())
        try:
            flow = start_device_flow(client)
        except Exception as exc:  # noqa: BLE001 - réponse de Google remontée telle quelle
            raise HTTPException(400, f"Google a refusé la demande de code : {exc}") from exc

        # L'identifiant n'est conservé qu'une fois Google l'ayant accepté : il
        # servira aussi à rafraîchir le jeton à chaque démarrage.
        save_client(config.youtube.oauth_client_file, client)
        return flow

    @app.post("/api/connect/oauth/poll")
    def oauth_poll(request: OAuthPollRequest) -> dict:
        """Deuxième étape : vérifier si l'utilisateur a validé, et si oui, stocker le jeton."""
        from ytmgc.sources.oauth import complete_device_flow, load_client

        client = load_client(config.youtube.oauth_client_file)
        if client is None:
            raise HTTPException(400, "Aucun identifiant client OAuth enregistré.")
        try:
            state, message = complete_device_flow(
                client, request.device_code, config.youtube.auth_file
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"Échec de la vérification : {exc}") from exc
        return {"state": state, "message": message}

    # ------------------------------------------------------------- sources

    @app.get("/api/sources")
    def list_sources() -> dict:
        """Sources analysables : bibliothèque, likes, et playlists de l'utilisateur.

        Les playlists engendrées par l'outil sont écartées : les analyser
        reviendrait à reclasser sa propre sortie.
        """
        from ytmgc.sources.ytmusic import SPECIAL_SOURCES

        special = [{"key": key, "label": label} for key, label in SPECIAL_SOURCES.items()]
        managed = {playlist_id for playlist_id, _ in repository.managed_playlists().values()}

        try:
            summaries = services.youtube_factory(config).list_playlist_summaries()
        except Exception:  # noqa: BLE001 - compte non joignable : sources spéciales seules
            return {"special": special, "playlists": [], "defaults": config.youtube.sources,
                    "reachable": False}

        playlists = [
            {
                "key": f"playlist:{item['playlist_id']}",
                "label": item["title"],
                "thumbnail": item.get("thumbnail"),
            }
            for item in summaries
            if item["playlist_id"] not in managed
        ]
        return {"special": special, "playlists": playlists,
                "defaults": config.youtube.sources, "reachable": True}

    @app.get("/api/sources/count")
    def count_source(source: str) -> dict:
        """Nombre de titres d'une source, mesuré puis mémorisé.

        Interrogé source par source depuis l'interface : les playlists
        répondent en un appel, la bibliothèque demande d'être parcourue, et
        rien ne doit bloquer l'affichage pendant ce temps.
        """
        from ytmgc.sources.ytmusic import validate_sources

        try:
            validate_sources([source])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        if source not in services.counts:
            try:
                services.counts[source] = services.youtube_factory(config).count_source(source)
            except Exception as exc:  # noqa: BLE001 - compte non joignable, source disparue
                raise HTTPException(400, f"Décompte impossible : {exc}") from exc
        return {"source": source, "count": services.counts[source]}

    # ------------------------------------------------------------ analyse

    @app.post("/api/analyse")
    def analyse(request: AnalyseRequest | None = None) -> dict:
        """Scanne les sources demandées puis les apparie à Discogs.

        Sans corps de requête, les sources de la configuration s'appliquent :
        l'appel reste utilisable tel quel hors de l'interface.
        """
        from ytmgc.sources.ytmusic import validate_sources

        if jobs.busy():
            raise HTTPException(409, "Un traitement est déjà en cours")
        # Une liste vide est un choix explicite, pas une absence de choix :
        # y substituer la configuration reviendrait à tout analyser alors que
        # l'utilisateur vient de tout décocher.
        requested = request.sources if request is not None else None
        try:
            sources = validate_sources(
                config.youtube.sources if requested is None else requested
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        def work(job: Job) -> dict:
            # Chaque analyse repart de zéro : sans cela, l'aperçu cumulerait les
            # sources des analyses précédentes, alors qu'il doit refléter la
            # sélection en cours.
            repository.clear_library()
            job.message = f"Lecture de {len(sources)} source(s) YouTube Music…"
            tracks: list[Track] = services.youtube_factory(config).scan(sources)
            repository.upsert_tracks(tracks)

            pending = repository.unclassified_tracks()
            job.total = len(pending)
            job.message = (
                f"{len(tracks)} titres lus, {len(pending)} à identifier. Interrogation de Discogs…"
            )

            def progress(track, _classification) -> None:
                job.progress += 1
                job.message = f"{track.label()}"

            stats = classify_tracks(
                pending, repository, services.discogs_factory(config), config,
                progress=progress, tag_source=services.lastfm_factory(config),
                # Les verdicts déjà rendus sont relus depuis le fichier : ils
                # priment sur Discogs et ne coûtent rien.
                verdicts=verdict_file.load(config.claude.verdicts_file),
            )
            job.message = stats.line()
            return {"scanned": len(tracks), "sources": sources, "summary": stats.line()}

        return jobs.start("analyse", work).to_dict()

    # -------------------------------------------------------- passe modèle

    def _judge():
        """Client du modèle, ou une erreur qui dit quoi faire."""
        judge = services.judge_factory(config)
        if judge is None:
            raise HTTPException(
                400,
                "Aucune clé d'API Claude. Définis ANTHROPIC_API_KEY puis relance "
                "l'interface pour activer le jugement par le modèle.",
            )
        return judge

    @app.get("/api/enrich/state")
    def enrich_state() -> dict:
        """Ce que coûterait la passe, et ce qui est déjà acquis.

        Appelé avant toute dépense : l'interface doit pouvoir annoncer un
        montant sans rien engager.
        """
        from ytmgc.enrich import load_pending, pending_subjects
        from ytmgc.sources.claude import estimate

        book = verdict_file.load(config.claude.verdicts_file)
        subjects = pending_subjects(repository, book)
        unsorted_only = pending_subjects(repository, book, only_unsorted=True)
        pending = load_pending(config.claude.pending_file)

        def scope(key: str, label: str, hint: str, count: int, **request) -> dict:
            """Une étendue proposée, avec son compte et son prix.

            Les trois sont décrites au même endroit pour que le montant affiché
            soit toujours celui de ce qui partira réellement.
            """
            approximate = estimate(count, config.claude)
            return {
                "key": key, "label": label, "hint": hint, "tracks": count,
                "dollars": approximate.dollars, "line": approximate.line(),
                "request": {"limit": 0, "only_unsorted": False, **request},
            }

        pilot = min(config.claude.pilot_size, len(subjects))
        return {
            "available": services.judge_factory(config) is not None,
            "model": config.claude.model,
            "verdicts": len(book),
            "verdicts_file": config.claude.verdicts_file,
            "pending_tracks": len(subjects),
            "scopes": [
                scope(
                    "pilot", f"Essai — {pilot} titres",
                    "À faire en premier : quelques centimes, et le résultat se lit "
                    "avant d'engager la suite.",
                    pilot, limit=config.claude.pilot_size,
                ),
                scope(
                    "unsorted", f"Titres non rangés — {len(unsorted_only)}",
                    "Seulement ceux que Discogs n'a pas su classer.",
                    len(unsorted_only), only_unsorted=True,
                ),
                scope(
                    "all", f"Toute la bibliothèque — {len(subjects)}",
                    "Tous les titres sans verdict.", len(subjects),
                ),
            ],
            "batch": (
                {"id": pending.batch_id, "tracks": pending.subjects,
                 "created_at": pending.created_at}
                if pending is not None else None
            ),
        }

    @app.post("/api/enrich")
    def enrich(request: EnrichRequest) -> dict:
        """Dépose un lot, attend son aboutissement, puis range la bibliothèque.

        Un lot déjà déposé est repris au lieu d'un nouveau : c'est du travail
        payé, et le redéposer le ferait payer deux fois.
        """
        from ytmgc.enrich import (
            apply_verdicts, collect, load_pending, pending_subjects, submit, wait,
        )
        from ytmgc.taxonomy import load_taxonomy

        if not request.confirm:
            raise HTTPException(400, "Confirmation requise : cette passe est facturée")
        if jobs.busy():
            raise HTTPException(409, "Un traitement est déjà en cours")
        judge = _judge()

        def work(job: Job) -> dict:
            pending = load_pending(config.claude.pending_file)
            if pending is None:
                book = verdict_file.load(config.claude.verdicts_file)
                subjects = pending_subjects(
                    repository, book, limit=request.limit,
                    only_unsorted=request.only_unsorted,
                )
                if not subjects:
                    job.message = "Tous les titres ont déjà un verdict."
                    return {"collected": 0, "applied": 0, "verdicts": len(book)}
                job.message = f"Dépôt de {len(subjects)} titre(s)…"
                pending = submit(judge, subjects, config)
            else:
                job.message = f"Reprise du lot {pending.batch_id}…"

            job.total = len(pending.chunks)
            job.message = f"Lot {pending.batch_id} en cours de traitement…"

            def on_status(state: str, counts: dict) -> None:
                job.progress = counts.get("succeeded", 0) + counts.get("errored", 0)
                job.message = (
                    f"Lot {pending.batch_id} : {job.progress}/{job.total} requête(s), {state}"
                )

            state = wait(judge, pending, on_status=on_status)
            if state != "ended":
                job.message = "Lot toujours en cours ; il sera repris au prochain lancement."
                return {"collected": 0, "applied": 0, "batch": pending.batch_id}

            book, stats = collect(judge, pending, config)
            stats.applied = apply_verdicts(repository, book, load_taxonomy(), config)
            job.message = stats.line()
            return {
                "collected": stats.collected,
                "applied": stats.applied,
                "problems": stats.problems,
                "verdicts": len(book),
            }

        return jobs.start("enrich", work).to_dict()

    @app.post("/api/lookup")
    def lookup(request: LookupRequest) -> dict:
        """Genre, style et ambiance d'un titre, tout de suite.

        Sert les titres ajoutés après la passe complète. Un titre déjà jugé est
        relu depuis le fichier, sans rien facturer.
        """
        from ytmgc.sources.claude import Subject
        from ytmgc.taxonomy import load_taxonomy

        book = verdict_file.load(config.claude.verdicts_file)
        key = verdict_file.entry_key(request.artist, request.title)
        known = book.get(key)
        if known is not None and not request.force:
            return {"verdict": _verdict_dict(known, load_taxonomy()), "cached": True}

        found = _judge().lookup(
            [Subject(artist=request.artist, title=request.title, album=request.album)]
        )
        if not found:
            raise HTTPException(502, "Le modèle n'a rien rendu d'exploitable pour ce titre.")
        book.add(found[0])
        verdict_file.save(book, config.claude.verdicts_file)
        return {"verdict": _verdict_dict(found[0], load_taxonomy()), "cached": False}

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
            from ytmgc.preview import filter_plans
            from ytmgc.sync import diff, managed_by_key

            client = services.youtube_factory(config)
            job.message = "Lecture des playlists existantes…"
            _, plans, _ = build_preview(
                repository, scoped, sort_mode=request.sort_mode,
                remote=client.list_playlists(),
            )

            # Le diff est refait après filtrage : les playlists écartées sont
            # laissées intactes, décocher signifiant « n'y touche pas ».
            plans = filter_plans(plans, request.excluded_playlists, request.excluded_tracks)
            known = {
                playlist_id: key
                for key, (playlist_id, _) in repository.managed_playlists().items()
            }
            remote = managed_by_key(
                client.list_playlists(), config.sync.marker,
                known=known, names={plan.name: plan.key for plan in plans},
            )
            actions = diff(plans, remote, scoped, untouched=frozenset(request.excluded_playlists))
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

    @app.get("/api/purge/candidates")
    def purge_candidates() -> dict:
        """Playlists que l'outil reconnaît comme siennes.

        Rien n'est supprimé ici : cette liste est là pour être examinée, la
        suppression étant la seule opération irréversible du projet.
        """
        from ytmgc.sync import managed_playlists

        try:
            remote = services.youtube_factory(config).list_playlists()
        except Exception as exc:  # noqa: BLE001 - compte non joignable
            raise HTTPException(400, f"Compte non joignable : {exc}") from exc

        known = {
            playlist_id: key for key, (playlist_id, _) in repository.managed_playlists().items()
        }
        return {
            "playlists": [
                {
                    "playlist_id": playlist.playlist_id,
                    "title": playlist.title,
                    "count": len(playlist.video_ids),
                    "thumbnail": playlist.thumbnail,
                    "key": known.get(playlist.playlist_id),
                }
                for playlist in sorted(
                    managed_playlists(remote, config.sync.marker), key=lambda item: item.title
                )
            ],
            "marker": config.sync.marker,
        }

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
                only=request.playlist_ids,
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


def _verdict_dict(verdict, taxonomy) -> dict:
    """Le verdict tel que l'interface l'affiche, style normalisé compris."""
    return {
        "artist": verdict.artist,
        "title": verdict.title,
        "genre": verdict.genre,
        "style": taxonomy.canonical_style(verdict.style) or verdict.style,
        "mood": verdict.mood,
        "confidence": verdict.confidence,
        "source": verdict.source,
        "note": verdict.note,
        "genre_text": taxonomy.describe_genre(verdict.genre),
        "style_text": taxonomy.describe_style(verdict.style),
        "mood_text": taxonomy.describe_mood(verdict.mood),
    }


def build_default_app(config_path: Path | None = None, config: Config | None = None) -> FastAPI:
    config = config if config is not None else load_config(config_path)
    return create_app(
        Services(
            config=config,
            repository=Repository(connect(config.store.path)),
            youtube_factory=_default_youtube,
            discogs_factory=_default_discogs,
            lastfm_factory=_default_lastfm,
            judge_factory=_default_judge,
            jobs=JobRunner(),
        )
    )
