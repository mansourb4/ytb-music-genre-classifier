"""Interface en ligne de commande.

Le pipeline est découpé en étapes reprenables, chacune persistée en base :

    ytmgc scan       # bibliothèque YouTube Music -> SQLite
    ytmgc classify   # SQLite -> Discogs -> genres/styles
    ytmgc plan       # genres/styles -> playlists souhaitées (hors ligne)
    ytmgc apply      # diff et écriture sur YouTube Music
    ytmgc status     # état d'avancement
    ytmgc review     # titres appariés avec un score incertain
    ytmgc tags       # tags Last.fm d'un titre, et ce qu'ils produisent
    ytmgc enrich --essai   # fait juger quelques titres, pour voir
    ytmgc enrich     # fait juger la bibliothèque par le modèle (par lots)
    ytmgc lookup     # genre, style et ambiance d'un titre, tout de suite
    ytmgc doublons   # morceaux présents sous plusieurs formes (gratuit)
    ytmgc export     # tous les titres à juger, en fichiers pour Claude.ai
    ytmgc importe    # lit une réponse de Claude.ai
    ytmgc suivi      # ce qui est jugé, ce qui reste
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
from ytmgc.models import MatchStatus, Track
from ytmgc.planner import plan_playlists
from ytmgc.store import Repository, connect
from ytmgc.sorting import DEFAULT_SORT_MODE, SORT_MODES, apply_sort_mode
from ytmgc.sync import apply as apply_actions
from ytmgc.sync import diff, managed_by_key, purge
from ytmgc.taxonomy import load_taxonomy
from ytmgc import verdicts


def _repository(config: Config) -> Repository:
    return Repository(connect(config.store.path))


def _youtube(config: Config):
    from ytmgc.sources.oauth import load_client
    from ytmgc.sources.ytmusic import YouTubeMusicClient

    return YouTubeMusicClient(
        config.youtube.auth_file,
        oauth_client=load_client(config.youtube.oauth_client_file),
    )


def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    from ytmgc.exclusions import apply_exclusions, excluded_tracks

    repository = _repository(config)
    youtube = _youtube(config)
    tracks = youtube.scan(config.youtube.sources)
    # Les exclusions choisies dans l'interface valent aussi ici : c'est la
    # même bibliothèque, et elle doit donner le même portrait.
    excluded = repository.excluded_playlists()
    scope = apply_exclusions(tracks, excluded_tracks(youtube, excluded))
    count = repository.upsert_tracks(scope.kept)
    # Un titre lu lors d'un scan antérieur à l'exclusion est encore en base.
    repository.remove_tracks([track.video_id for track in scope.removed])
    print(f"{count} titre(s) enregistré(s) depuis {', '.join(config.youtube.sources)}.")
    if scope.removed:
        print(f"{len(scope.removed)} titre(s) retiré(s) par {len(excluded)} playlist(s) exclue(s).")
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

    tag_source = None
    if config.lastfm.enabled and config.lastfm.api_key:
        from ytmgc.sources.lastfm import LastfmClient

        tag_source = LastfmClient(config.lastfm)

    stats = classify_tracks(
        tracks, repository, client, config, progress=progress, tag_source=tag_source,
        verdicts=verdicts.load(config.claude.verdicts_file) if config.claude.enabled else None,
    )
    print(stats.line())
    return 0


def _overrides(repository: Repository, config: Config) -> dict[str, str | None]:
    """Déplacements faits à la main, pour le tri « Par famille » seulement."""
    if config.taxonomy.axis != "plan":
        return {}
    from ytmgc.placements import library_overrides

    return library_overrides(repository.all_tracks(), config.taxonomy.placements_file)


def cmd_plan(args: argparse.Namespace, config: Config) -> int:
    repository = _repository(config)
    config = apply_sort_mode(config, args.tri)
    plans = plan_playlists(repository.classifications(), load_taxonomy(), config,
                           _overrides(repository, config),
                           artists={t.video_id: t.artists for t in repository.all_tracks()})
    if not plans:
        print("Aucune playlist à créer : aucun titre classé pour l'instant.")
        return 0
    total = sum(len(plan.video_ids) for plan in plans)
    print(f"{len(plans)} playlist(s) pour {total} affectation(s) de titres :\n")
    sizes = None
    if config.taxonomy.axis == "plan":
        from ytmgc.playlist_plan import load_plan

        sizes = load_plan(config.taxonomy.playlists_file, load_taxonomy())
    for plan in plans:
        warning = sizes.size_warning(len(plan.video_ids)) if sizes else None
        print(f"  {len(plan.video_ids):>5}  {plan.name}" + (f"   ⚠ {warning}" if warning else ""))
    return 0


def cmd_apply(args: argparse.Namespace, config: Config) -> int:
    repository = _repository(config)
    config = apply_sort_mode(config, args.tri)
    plans = plan_playlists(repository.classifications(), load_taxonomy(), config,
                           _overrides(repository, config),
                           artists={t.video_id: t.artists for t in repository.all_tracks()})
    if not plans:
        print("Rien à appliquer.")
        return 0

    client = _youtube(config)
    known = {
        playlist_id: key for key, (playlist_id, _) in repository.managed_playlists().items()
    }
    remote = managed_by_key(
        client.list_playlists(), config.sync.marker,
        known=known, names={plan.name: plan.key for plan in plans},
    )
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


def cmd_tags(args: argparse.Namespace, config: Config) -> int:
    """Montre les tags d'un titre et le classement qu'ils produisent.

    Sert à deux choses : vérifier qu'une clé Last.fm fonctionne avant de lancer
    une analyse complète, et comprendre après coup pourquoi un titre a atterri
    dans telle playlist plutôt que telle autre.
    """
    from ytmgc.sources.lastfm import LastfmClient
    from ytmgc.taxonomy import load_taxonomy

    track = Track(video_id="", title=args.titre, artists=(args.artiste,))
    tags = LastfmClient(config.lastfm).top_tags(track)

    if not tags:
        print(f"Aucun tag pour « {track.label()} ».")
        print("Le titre est inconnu de Last.fm, ou son libellé diffère trop.")
        print("Son classement s'appuiera sur les seuls styles de son album.")
        return 0

    taxonomy = load_taxonomy()
    print(f"Tags de « {track.label()} » :\n")
    for name, weight in tags:
        style = taxonomy.style_from_tag(name)
        retained = weight >= config.lastfm.min_tag_weight
        note = ""
        if style and retained:
            note = f"  -> style « {style} »"
        elif style:
            note = f"  -> style « {style} », écarté (poids < {config.lastfm.min_tag_weight})"
        print(f"  {weight:>3}  {name}{note}")

    styles = [
        style
        for name, weight in tags
        if weight >= config.lastfm.min_tag_weight and (style := taxonomy.style_from_tag(name))
    ][: config.lastfm.max_styles_per_track]

    scores = taxonomy.mood_scores(tags)
    mood = taxonomy.mood_from_tags(tags, minimum=config.lastfm.min_mood_weight)

    print()
    print(
        "Style retenu   :",
        " · ".join(dict.fromkeys(styles)) or "aucun (les styles de l'album feront foi)",
    )
    if scores:
        # Le détail du cumul : c'est lui qui départage, et il n'a rien d'évident
        # quand tous les tags d'humeur pèsent moins de dix.
        detail = ", ".join(f"{name} {total}" for name, total in
                           sorted(scores.items(), key=lambda item: -item[1]))
        print(f"Ambiances pesées : {detail}  (seuil {config.lastfm.min_mood_weight})")
    print("Ambiance       :", mood or "aucune (repli sur le style de l'album)")
    return 0


def _judge(config: Config):
    """Client du modèle. Import tardif : la dépendance est facultative."""
    from ytmgc.sources.claude import ClaudeClient

    return ClaudeClient(config.claude, load_taxonomy())


def _confirm(question: str) -> bool:
    """Demande confirmation avant de dépenser. Un non par défaut."""
    try:
        answer = input(f"{question} [o/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"o", "oui", "y", "yes"}


def cmd_enrich(args: argparse.Namespace, config: Config) -> int:
    """Fait juger les titres par le modèle, par lots, et garde les verdicts.

    C'est la seule commande de l'outil qui coûte de l'argent : elle annonce le
    montant avant de l'engager, ne redemande jamais un titre déjà jugé, et
    écrit son résultat dans un fichier texte qui lui survit.
    """
    from ytmgc.enrich import (
        apply_verdicts, collect, load_pending, pending_subjects, submit, wait,
    )
    from ytmgc.sources.claude import estimate

    repository = _repository(config)
    book = verdicts.load(config.claude.verdicts_file)

    # Le client n'est construit qu'au moment d'appeler : `--dry-run` doit
    # pouvoir annoncer un coût sans même exiger de clé d'API.
    client: list = []

    def judge():
        if not client:
            client.append(_judge(config))
        return client[0]

    pending = load_pending(config.claude.pending_file)
    if pending is None and args.resume:
        print("Aucun lot en attente : lance `ytmgc enrich` sans --resume.")
        return 0

    if pending is None:
        # L'essai prime sur --limit : demander les deux est une hésitation, et
        # la réponse la moins chère est la bonne.
        limit = config.claude.pilot_size if args.essai else args.limit
        subjects = pending_subjects(
            repository, book, limit=limit, only_unsorted=args.only_unsorted
        )
        if not subjects:
            if not repository.all_tracks():
                print("Bibliothèque vide : lance d'abord `ytmgc scan`.")
            else:
                print(
                    f"Rien à juger : les {len(book)} titre(s) connus ont déjà un verdict.\n"
                    f"Supprime une ligne de {config.claude.verdicts_file} pour en redemander un."
                )
            return 0

        approximate = estimate(len(subjects), config.claude)
        if not limit:
            from ytmgc.duplicates import find_duplicates

            report = find_duplicates(repository.all_tracks())
            if report.merged:
                print(f"{report.tracks} titres dans la bibliothèque, dont {report.merged} "
                      "doublon(s) jugé(s) une seule fois (détail : `ytmgc doublons`).")
        print(approximate.line())
        if args.essai:
            print(
                f"Essai : relis ensuite {config.claude.verdicts_file} avant d'engager la "
                "suite. Ces verdicts-là ne seront pas redemandés."
            )
        elif not args.limit and not args.only_unsorted and not len(book):
            # Premier passage : engager la bibliothèque entière sans avoir vu
            # un seul verdict, c'est payer avant de savoir si ça vaut le coup.
            print("Conseil : commence par `ytmgc enrich --essai` pour juger "
                  f"{config.claude.pilot_size} titres et lire le résultat.")
        if args.dry_run:
            print("\nRien n'a été envoyé (--dry-run).")
            return 0
        if not args.yes and not _confirm("Lancer la passe ?"):
            print("Abandon : rien n'a été envoyé.")
            return 0

        pending = submit(judge(), subjects, config)
        print(f"Lot {pending.batch_id} déposé ({pending.subjects} titre(s)).")
        print(f"Son identifiant est noté dans {config.claude.pending_file} :")
        print("tu peux interrompre et reprendre plus tard avec `ytmgc enrich --resume`.")
    else:
        print(f"Lot {pending.batch_id} en attente ({pending.subjects} titre(s)), déposé le "
              f"{pending.created_at or 'récemment'}.")

    if args.no_wait:
        print("Lot laissé en cours (--no-wait). Reprends avec `ytmgc enrich --resume`.")
        return 0

    def show(state: str, counts: dict[str, int]) -> None:
        done = counts.get("succeeded", 0) + counts.get("errored", 0)
        print(f"  {state} — {done}/{len(pending.chunks)} requête(s) traitée(s)")

    state = wait(judge(), pending, on_status=show)
    if state != "ended":
        print("Le lot n'est pas terminé. Reprends avec `ytmgc enrich --resume`.")
        return 0

    book, stats = collect(judge(), pending, config)
    stats.applied = apply_verdicts(repository, book, load_taxonomy(), config)
    print(stats.line())
    for problem in stats.problems:
        print(f"  échec : {problem}")
    print(f"{len(book)} verdict(s) au total dans {config.claude.verdicts_file}.")
    return 0


def cmd_lookup(args: argparse.Namespace, config: Config) -> int:
    """Genre, style et ambiance d'un titre, tout de suite.

    Pour les titres ajoutés après la passe complète : une réponse immédiate à
    quelques centimes, écrite dans le fichier de verdicts comme les autres, de
    sorte que la prochaine analyse la reprenne sans rien redemander.
    """
    from ytmgc.sources.claude import Subject

    book = verdicts.load(config.claude.verdicts_file)
    key = verdicts.entry_key(args.artiste, args.titre)

    known = book.get(key)
    if known is not None and not args.force:
        _show_verdict(known, config, already=True)
        return 0

    subject = Subject(artist=args.artiste, title=args.titre, album=args.album)
    found = _judge(config).lookup([subject])
    if not found:
        print("Le modèle n'a rien rendu d'exploitable pour ce titre.")
        return 1

    verdict = found[0]
    book.add(verdict)
    verdicts.save(book, config.claude.verdicts_file)
    _show_verdict(verdict, config, already=False)
    return 0


def _show_verdict(verdict, config: Config, *, already: bool) -> None:
    taxonomy = load_taxonomy()
    style = taxonomy.canonical_style(verdict.style) or verdict.style
    print(f"{verdict.artist} – {verdict.title}")
    print()
    print(f"  Genre     : {verdict.genre}")
    print(f"  Style     : {style}")
    print(f"  Ambiance  : {verdict.mood}")
    print(f"  Confiance : {verdict.confidence:.2f}  ({verdict.source})")
    if verdict.note:
        print(f"  Note      : {verdict.note}")
    print()
    if already:
        print(f"Verdict déjà connu, relu depuis {config.claude.verdicts_file} — rien n'a été "
              "facturé. Ajoute --force pour le redemander.")
    else:
        print(f"Écrit dans {config.claude.verdicts_file} : la prochaine analyse le reprendra.")


def cmd_doublons(args: argparse.Namespace, config: Config) -> int:
    """Montre comment la bibliothèque est dédoublonnée. Aucun appel réseau.

    C'est ce regroupement qui fixe le nombre de morceaux à juger : le voir
    permet de vérifier qu'on ne paiera pas deux fois la même chanson, et de
    signaler une variante que la normalisation laisserait passer.
    """
    from ytmgc.duplicates import find_duplicates

    report = find_duplicates(_repository(config).all_tracks())
    if not report.tracks:
        print("Bibliothèque vide : lance d'abord `ytmgc scan`.")
        return 0

    print(f"{report.tracks} titres dans la bibliothèque, {report.distinct} morceaux distincts.")
    print(f"{report.merged} doublon(s) fusionné(s) : ils ne seront ni jugés ni payés deux fois.")

    shown = report.groups if args.tout else report.groups[: args.limit]
    if shown:
        print("\nDoublons fusionnés (même morceau, plusieurs publications) :")
        for group in shown:
            first = group.tracks[0]
            print(f"\n  {first.artist} – {first.title}  ×{len(group.tracks)}")
            for track in group.tracks:
                origin = track.album or "sans album (clip ?)"
                print(f"      · {track.title}   [{', '.join(track.artists)} · {origin}]")
        if len(report.groups) > len(shown):
            print(f"\n  … et {len(report.groups) - len(shown)} autre(s) groupe(s) (--tout pour tout voir).")

    versions = report.versions if args.tout else report.versions[: args.limit]
    if versions:
        print("\nVersions proches gardées séparées (live, acoustique, remix…) :")
        for original, variant in versions:
            print(f"  {original.artist} – {original.title}   ≠   {variant.title}")
        if len(report.versions) > len(versions):
            print(f"  … et {len(report.versions) - len(versions)} autre(s) (--tout pour tout voir).")
        print("\nSi l'une d'elles est en réalité le même morceau, signale-la : la règle "
              "se corrige, pas la ligne.")
    return 0


def cmd_export(args: argparse.Namespace, config: Config) -> int:
    """Exporte tous les titres à juger, en fichiers à glisser dans Claude.ai."""
    from ytmgc.chat import export_library

    files = export_library(
        _repository(config), config, load_taxonomy(), only_unsorted=args.only_unsorted
    )
    if not files:
        print("Rien à exporter : tous les morceaux ont déjà un verdict.")
        return 0

    directory = Path(config.claude.export_dir).resolve()
    total = sum(len(f.keys) for f in files)
    print(f"{total} morceau(x) à juger, en {len(files)} fichier(s), dans :\n  {directory}\n")
    for f in files:
        print(f"  {f.name}   ({len(f.keys)} titres)")
    print(
        "\nPour chaque fichier :\n"
        "  1. ouvre une nouvelle conversation sur claude.ai ;\n"
        "  2. glisse-y le fichier et écris « Vas-y » ;\n"
        "  3. si Claude s'arrête avant la fin, écris « continue » ;\n"
        "  4. copie sa réponse (bouton « Copier » du bloc de code), puis :\n"
        "        pbpaste | ytmgc importe\n"
        "\nLes réponses s'importent dans n'importe quel ordre. `ytmgc suivi` montre "
        "où tu en es."
    )
    return 0


def cmd_importe(args: argparse.Namespace, config: Config) -> int:
    """Lit une réponse de Claude.ai, depuis un fichier ou l'entrée standard."""
    from ytmgc.chat import import_answer

    if args.fichier:
        text = Path(args.fichier).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            print("Colle la réponse de Claude, puis Ctrl+D :", file=sys.stderr)
        text = sys.stdin.read()

    result = import_answer(text, _repository(config), config, load_taxonomy())
    print(result.line())
    for reason in result.rejected:
        print(f"  écartée — {reason}")
    print(f"\nRésultats enregistrés dans {Path(config.claude.verdicts_file).resolve()}")
    if result.remaining:
        print(f"Reste {result.remaining} morceau(x) à juger — `ytmgc suivi` pour le détail.")
    else:
        print("Toute la bibliothèque est jugée.")
    return 0


def cmd_suivi(args: argparse.Namespace, config: Config) -> int:
    """Ce qui est jugé, ce qui reste, et où sont les résultats."""
    from ytmgc import progress
    from ytmgc.chat import export_progress

    tracks = _repository(config).all_tracks()
    if not tracks:
        print("Bibliothèque vide : lance d'abord `ytmgc scan`.")
        return 0

    book = verdicts.load(config.claude.verdicts_file)
    all_rows = progress.rows(tracks, book)
    judged = sum(1 for row in all_rows if row.judged)
    print(f"{judged} morceaux jugés sur {len(all_rows)}.")
    print(f"Résultats : {Path(config.claude.verdicts_file).resolve()}")

    files = export_progress(config)
    if files:
        print("\nFichiers du dernier export :")
        for f in files:
            state = "fait" if f.done else f"{f.judged}/{f.tracks}"
            print(f"  {f.name:<22} {state}")

    # Sans filtre demandé, le bilan suffit ; une recherche, elle, vaut demande
    # de liste, jugés et à juger confondus.
    show = args.show if args.show != "none" else ("all" if args.recherche else None)
    if show is not None:
        shown = progress.select(all_rows, show=show, query=args.recherche or "")
        print()
        for row in shown[: args.limit]:
            if row.judged:
                print(f"  ✓ {row.artist} – {row.title}  →  {row.genre} / {row.style} · {row.mood}")
            else:
                print(f"  · {row.artist} – {row.title}")
        if len(shown) > args.limit:
            print(f"  … et {len(shown) - args.limit} autre(s) (--limit pour en voir plus).")
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
    from ytmgc.web.links import access_url, in_codespace
    from ytmgc.web.security import generate_token

    if args.host is not None:
        config.web.host = args.host
    if args.port is not None:
        config.web.port = args.port
    if args.no_token:
        config.web.require_token = False

    if config.web.token_required() and not config.web.access_token:
        config.web.access_token = generate_token()
        print("Jeton d'accès engendré pour cette session.")
        print("Définis YTMGC_ACCESS_TOKEN pour en conserver un stable d'un démarrage à l'autre.")

    token = config.web.access_token if config.web.token_required() else ""
    print(f"\nInterface disponible sur :\n  {access_url(config.web.host, config.web.port, token)}\n")
    if token:
        print("Ce lien contient le jeton : ouvre-le tel quel, y compris depuis un téléphone.")
    if in_codespace():
        print("Codespace détecté : le port 8765 doit être transféré (onglet « Ports »).")
        print("Garde sa visibilité sur « Private » — le lien reste alors lié à ton compte GitHub.")

    uvicorn.run(
        build_default_app(args.config, config),
        host=config.web.host, port=config.web.port, log_level="warning",
    )
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


def cmd_gemini_export(args: argparse.Namespace, config: Config) -> int:
    """Écrit le prompt de révision complète et la table des numéros de titres."""
    from ytmgc import gemini, placements, playlist_review, verdicts
    from ytmgc.playlist_plan import load_plan
    from ytmgc.preview import build_preview

    repository = _repository(config)
    scoped = apply_sort_mode(config, "familles")
    summary, _, _ = build_preview(repository, scoped, sort_mode="familles")
    taxonomy = load_taxonomy()
    plan = load_plan(config.taxonomy.playlists_file, taxonomy)
    fixed = playlist_review.fixed_keys(verdicts.load(config.claude.verdicts_file),
                                       placements.load(config.taxonomy.placements_file))
    library = {t.video_id: t for t in repository.all_tracks()}
    if args.complet:
        prompt, rows = gemini.build_complete(summary, library)
    else:
        prompt, rows = gemini.build(summary, plan, library, fixed)
    folder = Path(args.dossier)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / gemini.PROMPT_FILE).write_text(prompt, encoding="utf-8")
    gemini.save_rows(rows, folder / gemini.TRACKS_FILE)
    print(f"{len(rows)} titres, {len(prompt)} caractères : {folder / gemini.PROMPT_FILE}")
    print("Glisse ce fichier dans Gemini, puis enregistre sa réponse entière dans un fichier "
          "et lance « gemini-importe ».")
    return 0


def cmd_gemini_import(args: argparse.Namespace, config: Config) -> int:
    """Lit la réponse de Gemini et l'applique : plan, déplacements, propositions."""
    from ytmgc import gemini

    rows = gemini.load_rows(Path(args.dossier) / gemini.TRACKS_FILE)
    answer = gemini.parse(Path(args.reponse).read_text(encoding="utf-8"))
    if not answer.finished:
        print("⚠ La réponse ne se termine pas par FIN : elle est peut-être coupée.")
    change = gemini.apply(answer, rows, config, load_taxonomy(), write=args.appliquer)
    for line in change.lines(rows):
        print(line)
    if not args.appliquer:
        print("\nRien n'a été écrit. Relance avec --appliquer pour appliquer ces changements.")
    else:
        print(f"\nÉcrit : {config.taxonomy.playlists_file}, {config.taxonomy.placements_file}. "
              "Recalcule l'aperçu pour voir le résultat.")
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

    tags_cmd = subparsers.add_parser(
        "tags", help="Afficher les tags Last.fm d'un titre et leur effet"
    )
    tags_cmd.add_argument("artiste")
    tags_cmd.add_argument("titre")
    tags_cmd.set_defaults(func=cmd_tags)

    enrich_cmd = subparsers.add_parser(
        "enrich", help="Faire juger la bibliothèque par le modèle (par lots, moitié prix)"
    )
    enrich_cmd.add_argument(
        "--essai", action="store_true",
        help="Ne juger qu'une poignée de titres (claude.pilot_size), pour lire le résultat "
             "avant d'engager la passe complète",
    )
    enrich_cmd.add_argument("--limit", type=int, default=0, help="Limiter le nombre de titres jugés")
    enrich_cmd.add_argument(
        "--only-unsorted", action="store_true",
        help="Ne juger que les titres que Discogs n'a pas su classer",
    )
    enrich_cmd.add_argument(
        "--dry-run", action="store_true", help="Annoncer le coût sans rien envoyer"
    )
    enrich_cmd.add_argument("--yes", action="store_true", help="Ne pas demander confirmation")
    enrich_cmd.add_argument(
        "--no-wait", action="store_true", help="Déposer le lot et rendre la main"
    )
    enrich_cmd.add_argument(
        "--resume", action="store_true", help="Reprendre un lot déjà déposé"
    )
    enrich_cmd.set_defaults(func=cmd_enrich)

    lookup_cmd = subparsers.add_parser(
        "lookup", help="Genre, style et ambiance d'un titre, tout de suite"
    )
    lookup_cmd.add_argument("artiste")
    lookup_cmd.add_argument("titre")
    lookup_cmd.add_argument("--album", default=None, help="Lève les homonymies")
    lookup_cmd.add_argument(
        "--force", action="store_true", help="Redemander même si un verdict existe"
    )
    lookup_cmd.set_defaults(func=cmd_lookup)

    doublons_cmd = subparsers.add_parser(
        "doublons", help="Morceaux présents sous plusieurs formes (aucun appel réseau)"
    )
    doublons_cmd.add_argument("--limit", type=int, default=20, help="Groupes affichés")
    doublons_cmd.add_argument("--tout", action="store_true", help="Tout afficher")
    doublons_cmd.set_defaults(func=cmd_doublons)

    export_cmd = subparsers.add_parser(
        "export", help="Exporter les titres à juger, en fichiers pour Claude.ai (gratuit)"
    )
    export_cmd.add_argument("--only-unsorted", action="store_true",
                            help="Seulement les titres que Discogs n'a pas su classer")
    export_cmd.set_defaults(func=cmd_export)

    importe_cmd = subparsers.add_parser("importe", help="Lire une réponse de Claude.ai")
    importe_cmd.add_argument("fichier", nargs="?", default=None,
                             help="Fichier contenant la réponse (sinon : entrée standard)")
    importe_cmd.set_defaults(func=cmd_importe)

    suivi_cmd = subparsers.add_parser("suivi", help="Ce qui est jugé, ce qui reste")
    group = suivi_cmd.add_mutually_exclusive_group()
    group.add_argument("--faits", dest="show", action="store_const", const="done",
                       help="Lister les morceaux jugés, avec leur verdict")
    group.add_argument("--a-faire", dest="show", action="store_const", const="todo",
                       help="Lister les morceaux qui restent à juger")
    suivi_cmd.add_argument("--recherche", default=None, help="Filtrer par artiste ou titre")
    suivi_cmd.add_argument("--limit", type=int, default=30)
    suivi_cmd.set_defaults(func=cmd_suivi, show="none")

    purge_cmd = subparsers.add_parser("purge", help="Supprimer les playlists générées par l'outil")
    purge_cmd.add_argument("--execute", action="store_true", help="Supprimer réellement (sinon : à blanc)")
    purge_cmd.set_defaults(func=cmd_purge)

    web = subparsers.add_parser("web", help="Lancer l'interface locale")
    web.add_argument("--host", default=None, help="Interface d'écoute (défaut : locale uniquement)")
    web.add_argument("--port", type=int, default=None)
    web.add_argument(
        "--no-token", action="store_true",
        help="Servir sans jeton même hors boucle locale (déconseillé)",
    )
    web.set_defaults(func=cmd_web)

    status = subparsers.add_parser("status", help="État d'avancement")
    status.set_defaults(func=cmd_status)

    gem_export = subparsers.add_parser(
        "gemini-exporte", help="Écrire le prompt de révision complète pour Gemini")
    gem_export.add_argument("--dossier", default="gemini")
    gem_export.add_argument("--complet", action="store_true",
                            help="Tri complet : Gemini range tout, sans playlists ni règles")
    gem_export.set_defaults(func=cmd_gemini_export)

    gem_import = subparsers.add_parser(
        "gemini-importe", help="Lire et appliquer la réponse de Gemini")
    gem_import.add_argument("reponse", help="Fichier contenant la réponse entière de Gemini")
    gem_import.add_argument("--dossier", default="gemini")
    gem_import.add_argument("--appliquer", action="store_true",
                            help="Écrire les changements (sinon : vérification seule)")
    gem_import.set_defaults(func=cmd_gemini_import)

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
