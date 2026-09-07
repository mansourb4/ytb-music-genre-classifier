"""Interface en ligne de commande.

Le pipeline est découpé en étapes reprenables, chacune persistée en base :

    ytmgc scan       # bibliothèque YouTube Music -> SQLite
    ytmgc classify   # SQLite -> Discogs -> genres/styles
    ytmgc plan       # genres/styles -> playlists souhaitées (hors ligne)
    ytmgc apply      # diff et écriture sur YouTube Music
    ytmgc status     # état d'avancement
    ytmgc review     # titres appariés avec un score incertain
    ytmgc purge      # supprime les playlists générées (annulation complète)
    ytmgc web        # interface locale : connexion, aperçu, application

`apply` est en mode « à blanc » par défaut : il faut `--execute` pour écrire.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ytmgc import __version__
from ytmgc.classifier import classify_tracks
from ytmgc.config import DEFAULT_CONFIG_PATH, Config, load_config
from ytmgc.models import MatchStatus
from ytmgc.planner import plan_playlists
from ytmgc.store import Repository, connect
from ytmgc.sorting import DEFAULT_SORT_MODE, SORT_MODES, apply_sort_mode
from ytmgc.sync import apply as apply_actions
from ytmgc.sync import diff, managed_by_key, purge
from ytmgc.taxonomy import load_taxonomy


def _repository(config: Config) -> Repository:
    return Repository(connect(config.store.path))


def _youtube(config: Config):
    from ytmgc.sources.ytmusic import YouTubeMusicClient

    return YouTubeMusicClient(config.youtube.auth_file)


def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    repository = _repository(config)
    tracks = _youtube(config).scan(config.youtube.sources)
    count = repository.upsert_tracks(tracks)
    print(f"{count} titre(s) enregistré(s) depuis {', '.join(config.youtube.sources)}.")
    return 0


def cmd_classify(args: argparse.Namespace, config: Config) -> int:
    from ytmgc.sources.discogs import DiscogsClient

    repository = _repository(config)
    tracks = repository.all_tracks() if args.all else repository.unclassified_tracks()
    if args.limit:
        tracks = tracks[: args.limit]
    if not tracks:
        print("Rien à classer. Lance d'abord `ytmgc scan`, ou utilise --all pour reclasser.")
        return 0

    client = DiscogsClient(config.discogs)
    processed = 0

    def progress(track, classification) -> None:
        nonlocal processed
        processed += 1
        if args.verbose:
            detail = "/".join(classification.styles) or "-"
            print(f"  [{processed}/{len(tracks)}] {track.label()} -> "
                  f"{classification.status.value} ({classification.score:.2f}) {detail}")

    stats = classify_tracks(tracks, repository, client, config, progress=progress)
    print(stats.line())
    return 0


def cmd_plan(args: argparse.Namespace, config: Config) -> int:
    repository = _repository(config)
    config = apply_sort_mode(config, args.tri)
    plans = plan_playlists(repository.classifications(), load_taxonomy(), config)
    if not plans:
        print("Aucune playlist à créer : aucun titre classé pour l'instant.")
        return 0
    total = sum(len(plan.video_ids) for plan in plans)
    print(f"{len(plans)} playlist(s) pour {total} affectation(s) de titres :\n")
    for plan in plans:
        print(f"  {len(plan.video_ids):>5}  {plan.name}")
    return 0


def cmd_apply(args: argparse.Namespace, config: Config) -> int:
    repository = _repository(config)
    config = apply_sort_mode(config, args.tri)
    plans = plan_playlists(repository.classifications(), load_taxonomy(), config)
    if not plans:
        print("Rien à appliquer.")
        return 0

    client = _youtube(config)
    remote = managed_by_key(client.list_playlists(), config.sync.marker)
    actions = diff(plans, remote, config)
    if not actions:
        print("YouTube Music est déjà à jour.")
        return 0

    log = apply_actions(
        actions, plans, client, config,
        dry_run=not args.execute,
        on_created=repository.remember_playlist,
    )
    for line in log:
        print(f"  {line}")
    if not args.execute:
        print(f"\n{len(actions)} action(s) simulée(s). Relance avec --execute pour écrire.")
    else:
        print(f"\n{len(actions)} action(s) appliquée(s).")
    return 0


def cmd_purge(args: argparse.Namespace, config: Config) -> int:
    """Annulation complète : supprime les playlists créées par l'outil."""
    repository = _repository(config)
    client = _youtube(config)
    log = purge(
        client.list_playlists(), client, config,
        dry_run=not args.execute,
        on_deleted=repository.forget_playlist,
    )
    if not log:
        print("Aucune playlist générée par l'outil sur ce compte.")
        return 0
    for line in log:
        print(f"  {line}")
    if not args.execute:
        print(f"\n{len(log)} suppression(s) simulée(s). Relance avec --execute pour supprimer.")
    else:
        print(f"\n{len(log)} playlist(s) supprimée(s).")
    return 0


def cmd_web(args: argparse.Namespace, config: Config) -> int:
    """Sert l'interface locale."""
    try:
        import uvicorn
    except ImportError:
        print(
            "L'interface web requiert des dépendances supplémentaires :\n"
            "  pip install \"ytmgc[web]\"",
            file=sys.stderr,
        )
        return 1

    from ytmgc.web.app import build_default_app

    print(f"Interface disponible sur http://{args.host}:{args.port}")
    uvicorn.run(build_default_app(args.config), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    repository = _repository(config)
    tracks = repository.all_tracks()
    counts = repository.counts_by_status()
    print(f"Titres scannés     : {len(tracks)}")
    print(f"  classés          : {counts.get('matched', 0)}")
    print(f"  à vérifier       : {counts.get('review', 0)}")
    print(f"  sans correspond. : {counts.get('unmatched', 0)}")
    print(f"  non traités      : {len(repository.unclassified_tracks())}")
    managed = repository.managed_playlists()
    print(f"Playlists gérées   : {len(managed)}")
    return 0


def cmd_review(args: argparse.Namespace, config: Config) -> int:
    repository = _repository(config)
    tracks = {track.video_id: track for track in repository.all_tracks()}
    pending = repository.classifications(MatchStatus.REVIEW)
    if not pending:
        print("Aucun titre en attente de vérification.")
        return 0
    for classification in pending[: args.limit]:
        track = tracks.get(classification.video_id)
        label = track.label() if track else classification.video_id
        styles = "/".join(classification.styles) or "-"
        print(f"  {classification.score:.2f}  {label}  ->  discogs:{classification.discogs_id}  {styles}")
    print(f"\n{len(pending)} titre(s) sous le seuil de {config.matching.min_score}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ytmgc",
        description="Organise une bibliothèque YouTube Music par genre et style Discogs.",
    )
    parser.add_argument("--version", action="version", version=f"ytmgc {__version__}")
    parser.add_argument(
        "-c", "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"Fichier de configuration (défaut : {DEFAULT_CONFIG_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Scanner la bibliothèque YouTube Music")
    scan.set_defaults(func=cmd_scan)

    classify = subparsers.add_parser("classify", help="Interroger Discogs et classer les titres")
    classify.add_argument("--all", action="store_true", help="Reclasser aussi les titres déjà traités")
    classify.add_argument("--limit", type=int, default=0, help="Limiter le nombre de titres traités")
    classify.add_argument("-v", "--verbose", action="store_true", help="Détailler chaque titre")
    classify.set_defaults(func=cmd_classify)

    tri_help = "Type de tri : " + ", ".join(f"{mode.key} ({mode.summary})" for mode in SORT_MODES)

    plan = subparsers.add_parser("plan", help="Afficher les playlists souhaitées (hors ligne)")
    plan.add_argument("--tri", default=DEFAULT_SORT_MODE, choices=[m.key for m in SORT_MODES], help=tri_help)
    plan.set_defaults(func=cmd_plan)

    apply_cmd = subparsers.add_parser("apply", help="Synchroniser les playlists sur YouTube Music")
    apply_cmd.add_argument("--tri", default=DEFAULT_SORT_MODE, choices=[m.key for m in SORT_MODES], help=tri_help)
    apply_cmd.add_argument("--execute", action="store_true", help="Écrire réellement (sinon : à blanc)")
    apply_cmd.set_defaults(func=cmd_apply)

    purge_cmd = subparsers.add_parser("purge", help="Supprimer les playlists générées par l'outil")
    purge_cmd.add_argument("--execute", action="store_true", help="Supprimer réellement (sinon : à blanc)")
    purge_cmd.set_defaults(func=cmd_purge)

    web = subparsers.add_parser("web", help="Lancer l'interface locale")
    web.add_argument("--host", default="127.0.0.1", help="Interface d'écoute (défaut : locale uniquement)")
    web.add_argument("--port", type=int, default=8765)
    web.set_defaults(func=cmd_web)

    status = subparsers.add_parser("status", help="État d'avancement")
    status.set_defaults(func=cmd_status)

    review = subparsers.add_parser("review", help="Lister les appariements incertains")
    review.add_argument("--limit", type=int, default=50)
    review.set_defaults(func=cmd_review)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        return args.func(args, config)
    except KeyboardInterrupt:
        print("\nInterrompu. L'avancement est conservé en base ; relance la même commande.")
        return 130
    except Exception as exc:  # noqa: BLE001 - le CLI présente les erreurs proprement
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
