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
    ytmgc telecharge # copie locale : un dossier par playlist, un m4a par titre
    ytmgc apple-music # range ces fichiers dans Apple Music (dossiers, playlists)
    ytmgc like --playlist NOM   # like tous les titres d'une playlist
    ytmgc apple-music-likes     # titres likés → favoris dans Apple Music
    ytmgc absents    # ce qui manque dans Apple Music, et pourquoi
    ytmgc synchro    # Apple Music maître : ses changements partout, puis les nouveautés
    ytmgc web        # interface locale : connexion, aperçu, application

`apply` est en mode « à blanc » par défaut : il faut `--execute` pour écrire.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from ytmgc import __version__
from ytmgc.classifier import classify_tracks
from ytmgc.config import DEFAULT_CONFIG_PATH, Config, load_config
from ytmgc.models import MatchStatus, Track
from ytmgc.planner import plan_playlists
from ytmgc.store import Repository, connect
from ytmgc.sorting import DEFAULT_SORT_MODE, SORT_MODES, apply_sort_mode
from ytmgc.sync import apply as apply_actions
from ytmgc.sync import adopt_matches, diff, managed_by_key, purge
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
    from ytmgc.exclusions import apply_exclusions, excluded_tracks, placed_keys

    repository = _repository(config)
    youtube = _youtube(config)
    tracks = youtube.scan(config.youtube.sources)
    # Les exclusions choisies dans l'interface valent aussi ici : c'est la
    # même bibliothèque, et elle doit donner le même portrait.
    excluded = repository.excluded_playlists()
    scope = apply_exclusions(tracks, excluded_tracks(youtube, excluded),
                             placed_keys(config.taxonomy.placements_file))
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
    if args.execute:
        adopt_matches(remote, (plan.key for plan in plans), repository.remember_playlist)
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


def cmd_telecharge(args: argparse.Namespace, config: Config) -> int:
    """Copie locale des playlists : dossiers, propriétés, téléchargements."""
    from ytmgc import download
    from ytmgc.preview import build_preview

    try:
        import mutagen  # noqa: F401
    except ImportError:
        print("Il manque le module mutagen : lance « pip install mutagen » puis recommence.",
              file=sys.stderr)
        return 1

    root = Path(args.dossier).expanduser()
    repository = _repository(config)
    tracks = {track.video_id: track for track in repository.all_tracks()}
    _, plans, _ = build_preview(repository, apply_sort_mode(config, args.tri), sort_mode=args.tri)
    styles = {item.video_id: item.styles for item in repository.classifications()}
    duplicates: list[str] = []
    wanted = download.targets(plans, tracks, styles, duplicates=duplicates)
    if not wanted:
        print("Aucune playlist : rien à télécharger.")
        return 0
    print("Lecture des fichiers déjà présents…", flush=True)
    unknown: list[Path] = []
    present = download.existing(root, download.read_id, unknown=unknown)
    adopted = download.adopt(wanted, tracks, present, unknown,
                             readable=lambda relative: download.readable(root / relative))
    present = {**present, **adopted}
    local = download.organise(wanted, present)
    to_retag = [v for v, path in present.items()
                if v in wanted and download.needs_tags(root / path, v, wanted[v].tags)]
    todo = list(local.downloads)
    if args.playlist:
        home = {v: item.tags.genre for v, item in wanted.items()}
        todo = [(v, path) for v, path in todo if args.playlist.lower() in home[v].lower()]
    if args.remplacer_sous:
        # Fichiers au son trop pauvre : retéléchargés à leur place définitive,
        # une fois les déplacements faits.
        # Un fichier déjà pris connecté au compte est au mieux de ce que
        # YouTube donne : le refaire ne servirait qu'à perdre du temps.
        low = sorted(v for v, path in present.items()
                     if v in wanted and (download.kbps(root / path) or 0) < args.remplacer_sous
                     and not download.signed_in(root / path))
        todo += [(v, wanted[v].path) for v in low]
        if low:
            print(f"  {len(low)} fichier(s) sous {args.remplacer_sous} kbps, à retélécharger")
    if args.limite:
        todo = todo[: args.limite]

    print(f"Dossier : {root}")
    print(f"  {len(plans)} playlist(s), {len(wanted)} titre(s)"
          + (f" ({len(duplicates)} doublon(s) écarté(s) : même artiste, même titre)"
             if duplicates else ""))
    if adopted:
        print(f"  {len(adopted)} fichier(s) venus d'ailleurs reconnus à leur nom : "
              "rangés au lieu d'être retéléchargés")
    print(f"  {local.up_to_date} déjà en place, {len(local.moves)} à déplacer ou renommer, "
          f"{len(to_retag)} aux propriétés à mettre à jour, {len(local.downloads)} à télécharger"
          + (f" (dont {len(todo)} cette fois)" if len(todo) != len(local.downloads) else ""))

    if args.essai:
        for move in local.moves:
            print(f"  déplacer  {move.source}  →  {move.target}")
        for _, path in todo[:20]:
            print(f"  télécharger  {path}")
        if len(todo) > 20:
            print(f"  … et {len(todo) - 20} autre(s)")
        print("\nEssai : rien n'a été modifié.")
        return 0

    changed = download.retag(root, wanted, present)
    if changed:
        print(f"Propriétés mises à jour sur {changed} fichier(s).")
    if local.moves:
        download.apply_moves(root, local.moves)
        print(f"{len(local.moves)} fichier(s) déplacé(s) ou renommé(s).")
    if not todo:
        print("Tout est déjà téléchargé.")
        return 0

    cookies_file = Path(args.cookies).expanduser() if args.cookies else None
    if cookies_file is not None and not cookies_file.is_file():
        print(f"Fichier de cookies introuvable : {cookies_file}", file=sys.stderr)
        return 1
    ytdlp = download.find_tool("yt-dlp", args.yt_dlp)
    if ytdlp is None:
        print(
            "yt-dlp introuvable. Installe-le (brew install yt-dlp ffmpeg), ou indique "
            "celui de Stacher avec --yt-dlp CHEMIN (Stacher → Réglages → Avancé).",
            file=sys.stderr,
        )
        return 1
    ffmpeg = download.find_tool("ffmpeg", args.ffmpeg)
    if ffmpeg is None:
        print("Attention : ffmpeg introuvable, yt-dlp risque d'échouer "
              "(brew install ffmpeg, ou --ffmpeg CHEMIN).")
    if download.find_tool("deno") is None:
        print("Attention : deno introuvable. yt-dlp en a besoin pour que YouTube accepte "
              "les téléchargements (sinon : erreurs 403). Installe-le : brew install deno")
    print(f"yt-dlp : {ytdlp}\nTéléchargement de {len(todo)} titre(s), "
          f"{'un par un' if args.parallele <= 1 else f'{args.parallele} à la fois'}. "
          "Ctrl+C pour arrêter : la reprise repart de là où tu en es.\n")

    def progress(index: int, total: int, track: Track, error: str | None) -> None:
        rate = None if error else download.kbps(root / wanted[track.video_id].path)
        state = f"ÉCHEC : {error}" if error else (f"ok, {rate} kbps" if rate else "ok")
        if error and download.rate_limited(error):
            state = "limité par YouTube : repris après la pause"
        if not error and track.video_id in report.alternates:
            state += ", autre version"
        print(f"  [{index}/{total}] {track.label()} — {state}", flush=True)

    alternatives = download.load_alternatives(
        Path(config.taxonomy.placements_file).with_name(download.ALTERNATIVES_FILE))
    sources = download.sources_for((tracks[v] for v, _ in todo), alternatives)
    if sources:
        print(f"{len(sources)} titre(s) pris depuis la version indiquée dans "
              f"{download.ALTERNATIVES_FILE}.\n")
    report = download.Report()
    search = None if args.sans_autre_version else download.ytmusic_search()
    batch, pauses = todo, 0
    while True:
        download.download(
            root, batch, tracks, wanted, ytdlp, ffmpeg=ffmpeg,
            cookies_browser=args.cookies_navigateur, cookies_file=cookies_file,
            pause=args.pause, workers=args.parallele, search=search, sources=sources,
            report=report,
            mark=download.mark_signed_in if (args.cookies_navigateur or args.cookies) else None,
            on_progress=progress,
        )
        if not report.rate_limited or pauses >= args.attentes_max:
            break
        # YouTube a limité le compte pour une heure au plus : on attend la levée
        # au lieu de faire échouer tous les titres restants.
        pauses += 1
        batch, report.pending, report.rate_limited = report.pending, [], False
        resume = time.localtime(time.time() + args.attente * 60)
        print(f"\nYouTube limite ton compte pour l'instant. Pause de {args.attente} min, "
              f"reprise vers {time.strftime('%H:%M', resume)} ({len(batch)} titre(s) "
              f"restant(s)). Laisse tourner, ou Ctrl+C pour reprendre plus tard.", flush=True)
        time.sleep(args.attente * 60)
    print(f"\n{report.downloaded} titre(s) téléchargé(s), {len(report.failed)} échec(s).")
    if report.alternates:
        print(f"{len(report.alternates)} titre(s) indisponible(s) remplacé(s) par une autre "
              "version du même morceau.")
    failures = download.write_failures(root, report.failed)
    if failures:
        print(f"Les échecs sont listés dans : {failures}")
        print("Ils seront retentés au prochain lancement.")
    if not (args.cookies_navigateur or args.cookies) and any(
            "Premium" in e for _, e in report.failed):
        print("Titres réservés à Premium : relance avec --cookies FICHIER (cookies exportés "
              "d'une fenêtre privée) ou --cookies-navigateur chrome.")
    if any("403" in error for _, error in report.failed):
        print("Erreurs 403 : mets yt-dlp à jour (brew upgrade yt-dlp) et vérifie que deno "
              "est installé (brew install deno).")
    if report.rate_limited:
        print(f"\nYouTube limite encore ton compte : {len(report.pending)} titre(s) non "
              "tentés. Relance dans une heure, avec --parallele 1 et --pause 5.")
        return 1
    if report.stopped:
        print(
            f"\nArrêt après {download.MAX_CONSECUTIVE_FAILURES} échecs d'affilée : YouTube "
            "bloque sans doute pour l'instant. Réessaie dans quelques heures, ou mets à "
            "jour yt-dlp (brew upgrade yt-dlp, ou bouton de mise à jour dans Stacher)."
        )
        return 1
    return 0


def cmd_apple_music(args: argparse.Namespace, config: Config) -> int:
    """Range les fichiers téléchargés dans Apple Music : dossiers et playlists."""
    from ytmgc import apple_music, download
    from ytmgc.preview import build_preview

    root = Path(args.dossier).expanduser().resolve()
    repository = _repository(config)
    tracks = {track.video_id: track for track in repository.all_tracks()}
    _, plans, _ = build_preview(repository, apply_sort_mode(config, args.tri), sort_mode=args.tri)
    styles = {item.video_id: item.styles for item in repository.classifications()}
    wanted = download.targets(plans, tracks, styles)
    print("Lecture des fichiers téléchargés…", flush=True)
    present = download.existing(root, download.read_id)
    state = apple_music.load_state(root)
    payload = apple_music.build_input(root, plans, wanted, present, state, dry_run=args.essai)
    if not payload["items"]:
        print(f"Aucun fichier dans {root} : lance d'abord « ytmgc telecharge ».")
        return 0
    print(f"{len(payload['items'])} titre(s), {len(payload['playlists'])} playlist(s). "
          "La première fois, macOS demande l'autorisation de piloter Music : accepte.\n",
          flush=True)

    result = apple_music.run_script(payload, on_line=lambda line: print(line, flush=True))
    if result.copied:
        print(
            "\nArrêt : Apple Music copie les fichiers ajoutés dans son propre dossier\n"
            f"  ({result.copied_to}),\n"
            "ce qui doublerait la place prise. Dans Music → Réglages → Fichiers, décoche\n"
            "« Copier les fichiers dans le dossier Music Media lors de l'ajout à la\n"
            "bibliothèque », puis relance. Le fichier de test a été retiré de la\n"
            "bibliothèque ; sa copie peut être supprimée de ce dossier.",
            file=sys.stderr,
        )
        return 1
    if not args.essai:
        state.known.update(result.known)
        state.families.update(payload["families"])
        apple_music.save_state(root, state)

    print("\nEssai : rien n'a été modifié. Ce qui serait fait :" if args.essai else "\nTerminé :")
    print(f"  {result.added} titre(s) ajouté(s) à la bibliothèque")
    if result.relocated:
        print(f"  {result.relocated} fichier(s) déplacé(s) retrouvé(s)")
    if result.retagged:
        print(f"  {result.retagged} genre(s) mis à jour")
    if result.created_folders:
        print(f"  dossiers créés : {', '.join(result.created_folders)}")
    if result.created_playlists:
        print(f"  {len(result.created_playlists)} playlist(s) créée(s)")
    print(f"  {result.track_adds} ajout(s) et {result.track_removals} retrait(s) dans les playlists")
    if result.missing:
        print(f"  {len(result.missing)} fichier(s) qu'Apple Music n'a pas pris, par exemple :")
        for path in result.missing[:5]:
            print(f"    {path}")
    if result.stale:
        print("  Playlists qui ne sont plus dans le plan, laissées telles quelles :")
        for key in result.stale:
            print(f"    {key}")
    return 0


def cmd_like(args: argparse.Namespace, config: Config) -> int:
    """Like chaque titre d'une playlist YouTube Music."""
    from ytmgc import likes

    client = _youtube(config)
    matches = likes.find_playlists(client.list_playlist_summaries(), args.playlist)
    if not matches:
        print(f"Aucune playlist dont le nom contient « {args.playlist} ».", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"Plusieurs playlists correspondent à « {args.playlist} », précise :")
        for match in matches:
            print(f"  {match['title']}")
        return 1
    playlist = matches[0]
    entries = client.playlist_entries(playlist["playlist_id"])
    print(f"Playlist « {playlist['title']} » : {len(entries)} titre(s).", flush=True)
    if args.verifier:
        print("Lecture de « Titres likés »…", flush=True)
        found = likes.check(entries, client.playlist_entries("LM"))
        print(f"« Titres likés » : {found.liked_total} titre(s).")
        print(f"Titres disponibles de la playlist : {found.wanted}, dont "
              f"{found.wanted - len(found.missing)} liké(s) et {len(found.missing)} "
              "absent(s) de « Titres likés ».")
        if found.first_rank is not None:
            print(f"Premier titre de la playlist : rang {found.first_rank + 1} dans "
                  "« Titres likés ».")
        print(f"Titres likés hors de l'ordre de la playlist : {found.out_of_order}.")
        thumbs = sum(1 for entry in found.missing if entry["liked"])
        print(f"Parmi les absents, pouce levé d'après la playlist : {thumbs} ; "
              f"pas de like : {len(found.missing) - thumbs}.")
        print(f"Absents par tranche de {likes.SLICE} titres, du haut de la playlist vers le bas :")
        for index, (size, gone) in enumerate(found.missing_by_slice):
            start = index * likes.SLICE + 1
            print(f"  titres {start}–{start + size - 1} : {gone} absent(s)")
        for entry in found.missing[:30]:
            thumb = "pouce levé" if entry["liked"] else "pas liké"
            print(f"  absent ({thumb}) : {entry['label']} ({entry['video_id']})")
        if len(found.missing) > 30:
            print(f"  … et {len(found.missing) - 30} autre(s).")
        return 0

    if args.reordonner:
        return _like_reorder(args, client, entries)

    def progress(index: int, total: int, entry: dict, error: str | None) -> None:
        state = f"ÉCHEC : {error}" if error else "liké"
        print(f"  [{index}/{total}] {entry['label']} — {state}", flush=True)

    report = likes.like_all(entries, client.like, limit=args.limite, pause=args.pause,
                            dry_run=args.essai, on_progress=progress)
    unavailable = (f", {report.unavailable} indisponible(s) sur YouTube (grisés, "
                   "impossibles à liker)" if report.unavailable else "")
    if args.essai:
        print(f"Essai : {report.liked} titre(s) à liker, {report.already} déjà liké(s)"
              f"{unavailable}. Rien n'a été modifié.")
        return 0
    print(f"\n{report.liked} titre(s) liké(s), {report.already} déjà liké(s), "
          f"{len(report.failed)} échec(s){unavailable}.")
    if report.stopped:
        print(f"Arrêt après {likes.MAX_CONSECUTIVE_FAILURES} échecs d'affilée : YouTube refuse "
              "sans doute pour l'instant. Relance plus tard la même commande.")
        return 1
    return 0


def _like_reorder(args: argparse.Namespace, client, entries: list[dict]) -> int:
    """`like --reordonner` : les likes de la playlist dans son ordre, vérifiés."""
    from ytmgc import likes

    if args.essai:
        report = likes.like_all(entries, client.like, limit=args.limite, dry_run=True)
        print(f"Essai : {report.liked + report.already} titre(s) seraient re-likés dans "
              "l'ordre de la playlist, par lots vérifiés. Rien n'a été modifié.")
        return 0
    report = likes.reorder(entries, like=client.like, unlike=client.unlike,
                           liked_ids=client.liked_ids, like_info=client.like_info,
                           limit=args.limite, pause=args.pause,
                           on_line=lambda line: print(line, flush=True))
    placed = report.kept + report.liked
    print(f"\n{placed} titre(s) dans l'ordre de la playlist en tête de « Titres likés »"
          f" ({report.unliked} retrait(s) de like, {report.retries} lot(s) refait(s)).")
    if report.unavailable:
        print(f"{report.unavailable} titre(s) indisponible(s) sur YouTube (grisés), ignorés.")
    if report.hidden:
        print(f"{len(report.hidden)} titre(s) likés mais que YouTube Music n'affiche pas dans "
              "« Titres likés » (vidéos qui ne sont pas des morceaux) :")
        for entry in report.hidden[:20]:
            print(f"  {entry['label']}")
        if len(report.hidden) > 20:
            print(f"  … et {len(report.hidden) - 20} autre(s).")
    if report.stuck:
        print(f"{len(report.stuck)} titre(s) dont YouTube refuse de retirer le like, laissés à "
              "leur place dans « Titres likés », hors de l'ordre :")
        for entry, thumb in report.stuck:
            state = "pouce levé" if thumb else "pouce baissé ou neutre d'après YouTube"
            print(f"  {entry['label']} ({entry['video_id']}) — {state}")
    if report.misplaced:
        print(f"{len(report.misplaced)} titre(s) qu'aucun essai n'a mis à leur place :")
        for entry in report.misplaced:
            print(f"  {entry['label']} ({entry['video_id']})")
    print("Pour reporter cet ordre dans les favoris d'Apple Music :\n"
          f"  python -m ytmgc apple-music-likes --reimporter --limite {placed}\n"
          "(si tu réordonnes plusieurs playlists, fais-le une fois à la fin, avec la "
          "somme de leurs nombres)")
    return 0


def cmd_apple_music_likes(args: argparse.Namespace, config: Config) -> int:
    """Met en favoris dans Apple Music les titres likés sur YouTube Music."""
    from ytmgc import apple_music

    root = Path(args.dossier).expanduser().resolve()
    state = apple_music.load_state(root)
    if not state.known:
        print("Apple Music ne connaît encore aucun titre de l'outil : lance d'abord "
              "« ytmgc apple-music ».", file=sys.stderr)
        return 1
    print("Lecture des titres likés sur YouTube Music…", flush=True)
    liked = _youtube(config).playlist_entries("LM")
    tracks = {track.video_id: track for track in _repository(config).all_tracks()}
    pids, absent = apple_music.favorite_ids(liked, tracks, state.known)
    print(f"{len(liked)} titre(s) liké(s), dont {len(pids)} présent(s) dans Apple Music.",
          flush=True)

    if args.limite:
        # Les N likes les plus récents : de quoi vérifier l'ordre sur peu de titres.
        pids = pids[: args.limite]
        by_pid = {pid: video_id for video_id, pid in state.known.items()}
        print("Ordre attendu dans les favoris d'Apple Music, du plus récent au plus ancien :")
        for rank, pid in enumerate(pids, 1):
            track = tracks.get(by_pid.get(pid, ""))
            print(f"  {rank}. {track.label() if track else pid}")
    if args.reimporter:
        print(f"Réimport de {len(pids)} titre(s) : retirés de la bibliothèque Music puis "
              "rajoutés dans l'ordre des likes (les fichiers ne bougent pas).", flush=True)
        result = apple_music.run_favorites(pids, reimport=True, interval=args.intervalle,
                                           dry_run=args.essai,
                                           on_line=lambda line: print(line, flush=True))
        if args.essai:
            print(f"\nEssai : {len(result.reimported)} titre(s) seraient réimportés.")
            return 0
        # Nouveaux identifiants Music : à retenir avant toute autre chose, pour
        # chaque identifiant YouTube du titre (les doublons partagent le leur).
        for video_id, pid in list(state.known.items()):
            if pid in result.reimported:
                state.known[video_id] = result.reimported[pid]
        apple_music.save_state(root, state)
        print(f"\n{len(result.reimported)} titre(s) réimporté(s) et remis en favoris. "
              "Retour dans leurs playlists…", flush=True)
        step = build_parser().parse_args(["apple-music", "--dossier", str(root)])
        return step.func(step, config)
    if args.retirer:
        result = apple_music.run_favorites(pids, remove=True, dry_run=args.essai,
                                           on_line=lambda line: print(line, flush=True))
        verb = "seraient retirés" if args.essai else "retirés"
        print(f"\n{result.removed} favori(s) {verb}. Synchronise l'iPhone, puis remets-les "
              "dans l'ordre avec « ytmgc apple-music-likes ».")
        return 0
    if args.reordonner and not args.essai:
        minutes = round(len(pids) * (args.intervalle + 0.1) / 60)
        print(f"Remise dans l'ordre : environ {minutes} min. Laisse Music ouvert.", flush=True)
    result = apple_music.run_favorites(pids, reorder=args.reordonner, interval=args.intervalle,
                                       dry_run=args.essai,
                                       on_line=lambda line: print(line, flush=True))
    repaired = ""
    if result.gone_pids and not args.essai:
        # Identifiants Music périmés (un doublon resté sur l'ancien, par
        # exemple) : oubliés, le titre est retrouvé par artiste et titre.
        stale = set(result.gone_pids)
        forgotten = [v for v, pid in state.known.items() if pid in stale]
        for video_id in forgotten:
            del state.known[video_id]
        apple_music.save_state(root, state)
        again, _ = apple_music.favorite_ids(liked, tracks, state.known)
        extra = [pid for pid in again if pid not in set(pids)]
        if extra:
            more = apple_music.run_favorites(extra, dry_run=False,
                                             on_line=lambda line: print(line, flush=True))
            result.favorited += more.favorited
            result.already += more.already
        repaired = (f"  {len(forgotten)} identifiant(s) Apple Music périmé(s) oublié(s) ; "
                    f"{len(extra)} titre(s) retrouvé(s) par artiste et titre")
    print("\nEssai : rien n'a été modifié. Ce qui serait fait :" if args.essai else "\nTerminé :")
    if args.reordonner:
        print(f"  {result.favorited} favori(s) remis dans l'ordre de YouTube Music")
    else:
        print(f"  {result.favorited} titre(s) mis en favoris, {result.already} l'étaient déjà")
    if repaired:
        print(repaired)
    elif result.gone:
        print(f"  {result.gone} titre(s) retiré(s) de la bibliothèque Music depuis : relance "
              "« ytmgc apple-music »")
    if absent:
        print(f"\n{len(absent)} titre(s) liké(s) absent(s) d'Apple Music (pas téléchargés, "
              "hors playlists ou indisponibles) :")
        for label in sorted(absent, key=str.casefold):
            print(f"  {label}")
    return 0


def cmd_absents(args: argparse.Namespace, config: Config) -> int:
    """Liste tout ce qui manque dans Apple Music : bibliothèque et likés."""
    from ytmgc import apple_music, download
    from ytmgc.preview import build_preview

    root = Path(args.dossier).expanduser().resolve()
    repository = _repository(config)
    library = {track.video_id: track for track in repository.all_tracks()}
    _, plans, _ = build_preview(repository, apply_sort_mode(config, args.tri), sort_mode=args.tri)
    wanted = download.targets(plans, library)
    print("Lecture des fichiers et des titres likés…", flush=True)
    present = download.existing(root, download.read_id)
    liked = [] if args.sans_likes else _youtube(config).playlist_entries("LM")
    state = apple_music.load_state(root)

    groups = apple_music.absent_titles(library, liked, wanted, present, state.known)
    total = sum(len(labels) for labels in groups.values())
    lines = [f"{total} titre(s) absent(s) d'Apple Music, sur {len(library)} titre(s) de la "
             f"bibliothèque et {len(liked)} liké(s).", ""]
    for reason, labels in groups.items():
        lines.append(f"{reason} : {len(labels)}")
        lines += [f"  {label}" for label in labels]
        lines.append("")
    report = "\n".join(lines)
    print("\n" + report)
    if root.is_dir():
        (root / "_absents.txt").write_text(report, encoding="utf-8")
        print(f"Liste enregistrée dans : {root / '_absents.txt'}")
    return 0


def _ask(prompt: str, choices: list[str]) -> int | None:
    """Question au clavier : indice du choix, ou None (Entrée seule, ou pas de clavier)."""
    print(prompt)
    for number, choice in enumerate(choices, 1):
        print(f"    {number}. {choice}")
    while True:
        try:
            raw = input("  Ton choix (Entrée pour passer) : ").strip()
        except EOFError:
            return None
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return int(raw) - 1
        print("  Réponds par un numéro de la liste.")


def _agree(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [o/N] ").strip().lower() in {"o", "oui", "y", "yes"}
    except EOFError:
        return False


def _pick_playlist(names: list[str]) -> str | None:
    """Choisit une playlist du plan en tapant une partie de son nom."""
    while True:
        try:
            text = input("  Nom (ou partie du nom) de la playlist : ").strip().casefold()
        except EOFError:
            return None
        if not text:
            return None
        matches = [n for n in names if text in n.casefold()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            print("  Aucune playlist ne correspond.")
            continue
        index = _ask("  Laquelle ?", matches[:15])
        if index is not None:
            return matches[index]


def cmd_synchro(args: argparse.Namespace, config: Config) -> int:
    """Apple Music maître : ses changements reportés partout, puis les nouveautés."""
    from ytmgc import apple_music, master, placements
    from ytmgc.matching.normalize import slugify
    from ytmgc.playlist_plan import load_plan

    root = Path(args.dossier).expanduser().resolve()
    state = apple_music.load_state(root)
    if not state.known:
        print("Apple Music ne connaît encore aucun titre de l'outil : lance d'abord "
              "« ytmgc apple-music ».", file=sys.stderr)
        return 1
    repository = _repository(config)
    tracks = {track.video_id: track for track in repository.all_tracks()}
    say = lambda line: print(line, flush=True)  # noqa: E731

    def photograph() -> "master.Photo":
        raw = apple_music._osascript(master.READ_SCRIPT, {}, on_line=say,
                                     popen=subprocess.Popen)
        tracked = (state.photo or {}).get("playlists", {}).keys()
        return master.photo_from_music(raw, state.known, state.families, tracked)

    def label(video_id: str) -> str:
        track = tracks.get(video_id)
        return track.label() if track else video_id

    after = photograph()
    if state.photo is None:
        print("Première synchro : je prends la photo d'Apple Music. Les synchros suivantes "
              "y compareront tes changements.")
    else:
        before = master.Photo.from_json(state.photo)
        changes = master.compare(before, after)
        if not changes:
            print("Aucun changement dans Apple Music depuis la dernière synchro.")
        else:
            decisions = _synchro_decide(changes, before, after, label, args, config)
            if decisions is None:
                return 1
            if args.essai:
                print("\nEssai : rien n'a été modifié.")
                return 0
            _synchro_apply(decisions, tracks, repository, config, slugify, placements,
                           master, load_plan)
    if args.essai:
        print("\nEssai : rien n'a été modifié.")
        return 0

    # La photo d'abord : si la suite échoue, tes changements ne seront pas
    # redemandés à la prochaine synchro.
    state.photo = after.to_json()
    apple_music.save_state(root, state)

    print("\n== Nouveautés de YouTube Music ==", flush=True)
    steps = [["scan"], ["classify"], ["apply", "--tri", args.tri]]
    for step in steps:
        parsed = build_parser().parse_args(step)
        if step[0] == "apply":
            parsed.func(parsed, config)  # aperçu, sans rien écrire
            if not _agree("Appliquer ces changements sur YouTube Music ?", args.oui):
                print("YouTube Music laissé tel quel.")
                continue
            parsed = build_parser().parse_args(step + ["--execute"])
        if parsed.func(parsed, config):
            print(f"Arrêt : l'étape « {step[0]} » a échoué.", file=sys.stderr)
            return 1

    download = ["telecharge", "--dossier", str(root), "--tri", args.tri]
    if args.cookies_navigateur:
        download += ["--cookies-navigateur", args.cookies_navigateur]
    for step in (download, ["apple-music", "--dossier", str(root), "--tri", args.tri],
                 ["apple-music-likes", "--dossier", str(root)]):
        parsed = build_parser().parse_args(step)
        if parsed.func(parsed, config):
            print(f"Arrêt : l'étape « {step[0]} » a échoué.", file=sys.stderr)
            return 1

    # Nouvelle photo, après les ajouts : ils ne sont pas des changements à toi.
    state = apple_music.load_state(root)
    state.photo = photograph().to_json()
    apple_music.save_state(root, state)
    print("\nSynchro terminée.")
    return 0


def _synchro_decide(changes, before, after, label, args, config):
    """Présente les changements, pose les questions, demande confirmation."""
    from ytmgc import master
    from ytmgc.playlist_plan import load_plan
    from ytmgc.taxonomy import load_taxonomy

    print("\n== Changements faits dans Apple Music ==")
    if changes.deleted:
        print(f"Supprimés de la bibliothèque ({len(changes.deleted)}) : retirés aussi de "
              "YouTube Music (bibliothèque, likes, playlists) et de la base.")
        for video_id in changes.deleted:
            print(f"  {label(video_id)}")
    for name in changes.created:
        print(f"Playlist créée : {name}")
    for old, new in changes.renamed:
        print(f"Playlist renommée : {old} → {new}")
    if changes.moves:
        print(f"Titres rangés ailleurs ({len(changes.moves)}) :")
        for video_id, name in changes.moves.items():
            print(f"  {label(video_id)} → {name}")
    if changes.favorites_added:
        print(f"Nouveaux favoris ({len(changes.favorites_added)}) : likés sur YouTube Music.")
        for video_id in changes.favorites_added:
            print(f"  {label(video_id)}")
    if changes.favorites_removed:
        print(f"Favoris retirés ({len(changes.favorites_removed)}) : like retiré sur "
              "YouTube Music.")
        for video_id in changes.favorites_removed:
            print(f"  {label(video_id)}")

    names = sorted({p.name for p in load_plan(config.taxonomy.playlists_file,
                                              load_taxonomy()).playlists}
                   | set(changes.created) | {new for _, new in changes.renamed})
    answers: dict[int, str] = {}
    if changes.questions:
        print(f"\n{len(changes.questions)} question(s) :")
    for index, question in enumerate(changes.questions):
        if args.essai:
            what = question.before[0] if question.video_id is None else label(question.video_id)
            print(f"  ? {question.kind} : {what}")
            continue
        if question.kind == "playlist supprimée":
            choice = _ask(f"\nLa playlist « {question.before[0]} » a été supprimée d'Apple Music.",
                          ["La supprimer aussi du plan et de YouTube Music (ses titres "
                           "sortent de toute playlist)",
                           "La recréer à la prochaine synchro"])
            if choice is not None:
                answers[index] = ["supprimer", "recréer"][choice]
        elif question.kind == "retiré":
            choice = _ask(f"\n« {label(question.video_id)} » a été retiré de "
                          f"« {', '.join(question.before)} » sans être rangé ailleurs.",
                          ["Le sortir de toute playlist « (aucune) »",
                           f"Le remettre dans « {question.before[0]} »",
                           "Le ranger dans une autre playlist"])
            if choice == 0:
                answers[index] = master.NOWHERE
            elif choice == 1:
                answers[index] = question.before[0]
            elif choice == 2:
                picked = _pick_playlist(names)
                if picked:
                    answers[index] = picked
        else:
            choice = _ask(f"\n« {label(question.video_id)} » est maintenant dans "
                          f"{' et '.join(question.after)}. Une seule playlist par titre : "
                          "laquelle garder ?", question.after)
            if choice is not None:
                answers[index] = question.after[choice]
    unanswered = sum(1 for i in range(len(changes.questions)) if i not in answers)
    if unanswered and not args.essai:
        print(f"\n{unanswered} question(s) sans réponse : ces titres reprendront leur place "
              "d'avant à la fin de la synchro.")

    decisions = master.decide(changes, before, after, answers)
    if args.essai:
        return decisions
    if not _agree("\nReporter ces changements sur YouTube Music et dans tes classements ?",
                  args.oui):
        print("Rien n'a été modifié. Relance la synchro quand tu veux.")
        return None
    return decisions


def _synchro_apply(decisions, tracks, repository, config, slugify, placements, master,
                   load_plan):
    """Écrit les décisions : plan, déplacements, base, likes et bibliothèque YouTube."""
    from ytmgc.taxonomy import load_taxonomy

    plan_file = Path(config.taxonomy.playlists_file)
    existing = {p.name for p in load_plan(str(plan_file), load_taxonomy()).playlists}
    for name in decisions.created:
        if name not in existing:
            master.add_playlist(plan_file, name, "Créée dans Apple Music.")
    managed = repository.managed_playlists()
    book = placements.load(config.taxonomy.placements_file)
    for old, new in decisions.renamed:
        master.rename_playlist(plan_file, old, new)
        # La playlist YouTube garde son identifiant : seule sa clé change, et
        # l'application la renommera au lieu d'en créer une autre.
        row = managed.get("plan/" + slugify(old))
        if row:
            repository.forget_playlist(row[0])
            repository.remember_playlist("plan/" + slugify(new), row[0], new)
        for entry in list(book):
            if entry.playlist == old:
                book.put(_track_of(entry), new)
    for name in decisions.removed:
        master.remove_playlist(plan_file, name)
    missing = 0
    for video_id, target in decisions.placements.items():
        track = tracks.get(video_id)
        if track is None:
            missing += 1
            continue
        book.put(track, target)
    placements.save(book, config.taxonomy.placements_file)
    if missing:
        print(f"{missing} titre(s) inconnu(s) de la base : déplacement non inscrit.")

    client = _youtube(config)
    for video_id in decisions.like:
        client.like(video_id)
    for video_id in decisions.unlike:
        try:
            client.unlike(video_id)
        except Exception as exc:  # noqa: BLE001 - un like absent n'arrête rien
            print(f"  like non retiré pour {video_id} : {exc}")
    if decisions.deleted:
        removed = client.remove_from_library(decisions.deleted)
        repository.remove_tracks(list(decisions.deleted))
        print(f"{removed} titre(s) retiré(s) de la bibliothèque YouTube Music, "
              f"{len(decisions.deleted)} de la base.")
    print(f"Classements mis à jour : {len(decisions.placements)} titre(s), "
          f"{len(decisions.created)} playlist(s) créée(s), {len(decisions.renamed)} renommée(s), "
          f"{len(decisions.removed)} supprimée(s). Likes : +{len(decisions.like)} "
          f"−{len(decisions.unlike)}.")


def _track_of(entry) -> Track:
    """Titre minimal tiré d'une ligne de placements, pour la réécrire."""
    return Track("", entry.title, (entry.artist,))


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
    if args.a_verifier:
        prompt, rows = gemini.build_unsure(summary, plan)
    elif args.complet:
        prompt, rows = gemini.build_complete(summary, library, plan)
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

    telecharge_cmd = subparsers.add_parser(
        "telecharge", help="Télécharger les playlists en m4a, un dossier par playlist")
    telecharge_cmd.add_argument("--dossier", default="~/Music/Playlists",
                                help="Dossier racine (défaut : %(default)s)")
    telecharge_cmd.add_argument("--tri", default=DEFAULT_SORT_MODE, choices=[m.key for m in SORT_MODES],
                                help="Type de tri, comme dans l'aperçu (défaut : %(default)s)")
    telecharge_cmd.add_argument("--essai", action="store_true",
                                help="Montrer ce qui serait fait, sans rien toucher")
    telecharge_cmd.add_argument("--playlist", default=None,
                                help="Ne télécharger que les playlists dont le nom contient ce texte")
    telecharge_cmd.add_argument("--limite", type=int, default=0,
                                help="Ne télécharger que ce nombre de titres (pour tester)")
    telecharge_cmd.add_argument("--pause", type=float, default=2.0,
                                help="Secondes d'attente entre deux titres (défaut : %(default)s)")
    telecharge_cmd.add_argument("--parallele", type=int, default=3,
                                help="Titres téléchargés en même temps (défaut : %(default)s ; "
                                     "1 si YouTube bloque)")
    telecharge_cmd.add_argument("--remplacer-sous", type=int, default=0, metavar="KBPS",
                                help="Retélécharger les fichiers déjà là dont le son est "
                                     "sous ce débit (ex. 100)")
    telecharge_cmd.add_argument("--attente", type=int, default=65, metavar="MINUTES",
                                help="Pause quand YouTube limite le compte (défaut : %(default)s)")
    telecharge_cmd.add_argument("--attentes-max", type=int, default=4,
                                help="Nombre de pauses avant d'abandonner (défaut : %(default)s)")
    telecharge_cmd.add_argument("--sans-autre-version", action="store_true",
                                help="Ne pas chercher d'autre version d'un titre indisponible")
    telecharge_cmd.add_argument("--yt-dlp", dest="yt_dlp", default=None,
                                help="Chemin de yt-dlp (sinon : cherché automatiquement)")
    telecharge_cmd.add_argument("--ffmpeg", default=None,
                                help="Chemin de ffmpeg (sinon : cherché automatiquement)")
    telecharge_cmd.add_argument("--cookies", default=None, metavar="FICHIER",
                                help="Fichier cookies.txt exporté d'une fenêtre de navigation "
                                     "privée connectée à YouTube Music")
    telecharge_cmd.add_argument("--cookies-navigateur", default=None,
                                help="Utiliser la session YouTube d'un navigateur (safari, chrome…)")
    telecharge_cmd.set_defaults(func=cmd_telecharge)

    apple_cmd = subparsers.add_parser(
        "apple-music", help="Ranger les fichiers téléchargés dans Apple Music (Mac)")
    apple_cmd.add_argument("--dossier", default="~/Music/Playlists",
                           help="Dossier des fichiers (défaut : %(default)s)")
    apple_cmd.add_argument("--tri", default=DEFAULT_SORT_MODE, choices=[m.key for m in SORT_MODES],
                           help="Type de tri, comme dans l'aperçu (défaut : %(default)s)")
    apple_cmd.add_argument("--essai", action="store_true",
                           help="Montrer ce qui serait fait, sans rien toucher")
    apple_cmd.set_defaults(func=cmd_apple_music)

    like_cmd = subparsers.add_parser("like", help="Liker tous les titres d'une playlist")
    like_cmd.add_argument("--playlist", required=True,
                          help="Nom de la playlist (ou une partie du nom)")
    like_cmd.add_argument("--essai", action="store_true",
                          help="Compter ce qui serait liké, sans rien toucher")
    like_cmd.add_argument("--pause", type=float, default=1.0,
                          help="Secondes entre deux likes (défaut : %(default)s)")
    like_cmd.add_argument("--reordonner", action="store_true",
                          help="Retirer puis remettre les likes de la playlist, déjà likés "
                               "compris, pour que « Titres likés » suive son ordre")
    like_cmd.add_argument("--limite", type=int, default=0,
                          help="Seulement les N premiers titres de la playlist (pour tester)")
    like_cmd.add_argument("--verifier", action="store_true",
                          help="Comparer la playlist et « Titres likés », sans rien toucher")
    like_cmd.set_defaults(func=cmd_like)

    likes_cmd = subparsers.add_parser(
        "apple-music-likes", help="Mettre en favoris dans Apple Music les titres likés (Mac)")
    likes_cmd.add_argument("--dossier", default="~/Music/Playlists",
                           help="Dossier des fichiers (défaut : %(default)s)")
    likes_cmd.add_argument("--essai", action="store_true",
                           help="Compter ce qui serait fait, sans rien toucher")
    likes_cmd.add_argument("--limite", type=int, default=0,
                           help="Seulement les N likes les plus récents (pour tester l'ordre)")
    likes_cmd.add_argument("--reimporter", action="store_true",
                           help="Retirer puis rajouter les titres likés à la bibliothèque, "
                                "dans l'ordre des likes : Music trie ses favoris par date "
                                "d'ajout")
    likes_cmd.add_argument("--retirer", action="store_true",
                           help="Seulement retirer ces favoris (pour vider l'iPhone avant de "
                                "les remettre dans l'ordre)")
    likes_cmd.add_argument("--reordonner", action="store_true",
                           help="Refaire tous les favoris, pour qu'Apple Music les montre "
                                "dans l'ordre des likes de YouTube Music")
    likes_cmd.add_argument("--intervalle", type=float, default=1.0,
                           help="Secondes entre deux favoris, pour que leurs dates se "
                                "suivent (défaut : %(default)s)")
    likes_cmd.set_defaults(func=cmd_apple_music_likes)

    absents_cmd = subparsers.add_parser(
        "absents", help="Lister ce qui manque dans Apple Music (bibliothèque et likés)")
    absents_cmd.add_argument("--dossier", default="~/Music/Playlists",
                             help="Dossier des fichiers (défaut : %(default)s)")
    absents_cmd.add_argument("--tri", default=DEFAULT_SORT_MODE,
                             choices=[m.key for m in SORT_MODES],
                             help="Type de tri, comme dans l'aperçu (défaut : %(default)s)")
    absents_cmd.add_argument("--sans-likes", action="store_true",
                             help="Ne pas lire les titres likés (hors ligne)")
    absents_cmd.set_defaults(func=cmd_absents)

    synchro_cmd = subparsers.add_parser(
        "synchro", help="Apple Music maître : reporter ses changements, puis les nouveautés")
    synchro_cmd.add_argument("--dossier", default="~/Music/Playlists",
                             help="Dossier des fichiers (défaut : %(default)s)")
    synchro_cmd.add_argument("--tri", default=DEFAULT_SORT_MODE,
                             choices=[m.key for m in SORT_MODES],
                             help="Type de tri, comme dans l'aperçu (défaut : %(default)s)")
    synchro_cmd.add_argument("--cookies-navigateur", default=None,
                             help="Session YouTube d'un navigateur pour les téléchargements")
    synchro_cmd.add_argument("--essai", action="store_true",
                             help="Montrer les changements détectés, sans rien modifier")
    synchro_cmd.add_argument("--oui", action="store_true",
                             help="Ne pas demander de confirmation (les questions restent)")
    synchro_cmd.set_defaults(func=cmd_synchro)

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
    gem_export.add_argument("--a-verifier", action="store_true",
                            help="Seulement les titres à vérifier, à ranger dans les playlists")
    gem_export.add_argument("--complet", action="store_true",
                            help="Tri complet : Gemini range tout, inspiré des playlists actuelles")
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
