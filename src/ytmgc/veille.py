"""La veille des likes : de ton pouce levé à Apple Music, en passant par toi.

Une boucle, lancée en fond sur le Mac (`ytmgc veille`, gardée en vie par
launchd) :

1. Toutes les `interval` secondes, le haut de « Titres likés » est relu.
   Un titre nouveau déjà rangé (ou doublon d'un titre rangé) est seulement
   mis en favori ; un titre vraiment nouveau est analysé, tenu hors playlist,
   et proposé sur Telegram.
2. Entre deux relectures, Telegram est écouté : les boutons répondent tout de
   suite (familles, playlists), et un choix est inscrit aussitôt.
3. Dès qu'un choix est fait, la chaîne habituelle tourne : playlists YouTube
   Music, téléchargement, Apple Music, favoris. Le message dit ensuite si le
   titre est arrivé dans Apple Music.

Les dépendances (YouTube, Telegram, base, chaîne) arrivent par `Services`,
pour que la logique se teste sans réseau.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ytmgc import nouveautes, placements
from ytmgc.download import song_key
from ytmgc.models import Classification, Track
from ytmgc.nouveautes import NOWHERE, Pending, State, escape
from ytmgc.telegram import Bot

#: Titres relus en haut de « Titres likés » à chaque passage.
LOOK = 200
#: Essais de la chaîne pour un titre choisi, avant d'abandonner.
MAX_ATTEMPTS = 3


@dataclass
class Services:
    #: Haut de « Titres likés » (tout, avec None), du plus récent au plus ancien.
    liked: Callable[[int | None], list[Track]]
    tracks: Callable[[], list[Track]]
    save_tracks: Callable[[list[Track]], None]
    classify: Callable[[list[Track]], None]
    classifications: Callable[[], Iterable[Classification]]
    #: Plan : nom de playlist → titres, dans l'ordre du plan.
    plans: Callable[[], Mapping[str, Iterable[str]]]
    placements_file: Path
    #: La chaîne ; `full` : avec playlists, téléchargement et Apple Music,
    #: sinon les seuls favoris. Rend les erreurs rencontrées.
    pipeline: Callable[[bool], list[str]]
    in_apple: Callable[[str], bool]
    bot: Bot
    chat: str
    log: Callable[[str], None] = print


def _track(pending: Pending) -> Track:
    return Track(pending.video_id, pending.title, (pending.artist,) if pending.artist else ())


def check(state: State, s: Services) -> bool:
    """Relit les likes ; propose les nouveaux. Rend True s'il faut des favoris."""
    if not state.started:
        state.seen = {t.video_id for t in s.liked(None)}
        s.log(f"Premier passage : {len(state.seen)} like(s) existants, laissés tels quels.")
        return False
    new = [t for t in s.liked(LOOK) if t.video_id not in state.seen]
    s.log(f"Relecture des likes : {len(new)} nouveau(x).")
    if not new:
        return False

    plans = s.plans()
    state.names = list(plans)
    book = placements.load(s.placements_file)
    known = {t.video_id: t for t in s.tracks()}
    planned = {v for ids in plans.values() for v in ids}
    songs = {song_key(known[v]) for v in planned if v in known}
    fresh, favorites = [], False
    for track in new:
        if (track.video_id in planned or song_key(track) in songs
                or book.get(placements.entry_key(track.artist, track.title)) is not None):
            s.log(f"Déjà rangé : {track.label()} (mis en favori)")
            favorites = True
        else:
            fresh.append(track)

    if fresh:
        s.save_tracks(fresh)
        s.classify(fresh)
        plans = s.plans()
        state.names = list(plans)
        ids = {t.video_id for t in fresh}
        classes = {c.video_id: c for c in s.classifications() if c.video_id in ids}
        for track in fresh:
            proposal = nouveautes.proposal_for(track.video_id, plans)
            # Tenu hors playlist jusqu'à ta réponse : rien ne se range sans toi.
            book.put(track, NOWHERE)
            pending = Pending(track.video_id, track.artist, track.title,
                              nouveautes.describe(track, classes.get(track.video_id), proposal),
                              proposal)
            state.add(pending)
            s.log(f"Nouveau like : {track.label()} → proposé : {proposal or 'aucune'}")
        placements.save(book, s.placements_file)
    state.seen.update(t.video_id for t in new)
    return favorites


def send_unsent(state: State, s: Services) -> None:
    """Envoie les propositions pas encore parties (Telegram injoignable avant)."""
    for key, pending in state.pending.items():
        if pending.message_id is None:
            pending.message_id = s.bot.send(s.chat, pending.text,
                                            nouveautes.first_buttons(state, key))
            s.log(f"Proposition envoyée sur Telegram : {_track(pending).label()}")


def handle(state: State, s: Services, updates: Iterable[dict]) -> int:
    """Traite les boutons touchés. Rend le nombre de choix faits."""
    decided = 0
    for update in updates:
        state.offset = int(update["update_id"]) + 1
        message = update.get("message")
        if message and str(message.get("chat", {}).get("id")) == s.chat:
            s.bot.send(s.chat, status(state))
            continue
        query = update.get("callback_query")
        if not query:
            continue
        if str(query.get("message", {}).get("chat", {}).get("id")) != s.chat:
            s.bot.answer(query["id"])
            continue
        answer = nouveautes.interpret(state, query.get("data") or "")
        if answer is None:
            s.bot.answer(query["id"], "Déjà traité.")
            continue
        pending = state.pending[answer.key]
        if answer.rows is not None:
            s.bot.edit(s.chat, pending.message_id, pending.text, answer.rows)
            s.bot.answer(query["id"])
            continue
        book = placements.load(s.placements_file)
        book.put(_track(pending), answer.decision)
        placements.save(book, s.placements_file)
        s.bot.answer(query["id"], "Noté.")
        if answer.decision == NOWHERE:
            s.bot.edit(s.chat, pending.message_id, pending.text + "\n\n👌 Laissé hors playlists.")
            del state.pending[answer.key]
            s.log(f"Hors playlists : {_track(pending).label()}")
        else:
            pending.chosen = answer.decision
            s.bot.edit(s.chat, pending.message_id, pending.text +
                       f"\n\n⏳ Rangé dans <b>{escape(answer.decision)}</b> : téléchargement, puis "
                       "Apple Music…")
            s.log(f"Choisi : {_track(pending).label()} → {answer.decision}")
            decided += 1
    return decided


def finish(state: State, s: Services, favorites: bool, checked: bool = True) -> None:
    """Fait tourner la chaîne pour les titres choisis, et dit ce qu'il en est.

    Un titre déjà manqué n'est réessayé qu'au passage suivant (`checked`), pas
    à chaque tour de boucle.
    """
    chosen = {k: p for k, p in state.pending.items()
              if p.chosen and (checked or not p.attempts)}
    if not chosen and not favorites:
        return
    errors = s.pipeline(bool(chosen))
    for key, pending in chosen.items():
        if s.in_apple(pending.video_id):
            s.bot.edit(s.chat, pending.message_id, pending.text +
                       f"\n\n✅ Dans <b>{escape(pending.chosen)}</b>, téléchargé et ajouté à Apple "
                       "Music, en favori. Il arrivera sur l'iPhone à la prochaine synchro.")
            del state.pending[key]
            continue
        pending.attempts += 1
        detail = escape("; ".join(errors) or "le fichier n'est pas arrivé")
        if pending.attempts >= MAX_ATTEMPTS:
            s.bot.edit(s.chat, pending.message_id, pending.text +
                       f"\n\n❌ Rangé dans <b>{escape(pending.chosen)}</b> sur YouTube Music, mais pas "
                       f"arrivé dans Apple Music après {MAX_ATTEMPTS} essais ({detail}). "
                       "Regarde « _échecs.txt » dans le dossier Playlists.")
            del state.pending[key]
        else:
            s.bot.edit(s.chat, pending.message_id, pending.text +
                       f"\n\n⚠️ Pas encore dans Apple Music ({detail}) : nouvel essai au "
                       "prochain passage.")


def status(state: State) -> str:
    waiting = [p for p in state.pending.values() if not p.chosen]
    working = [p for p in state.pending.values() if p.chosen]
    lines = [f"Veille active. {len(waiting)} titre(s) attendent ton choix, "
             f"{len(working)} en cours de rangement."]
    lines += [f"• {escape(_track(p).label())}" for p in waiting[:10]]
    return "\n".join(lines)


def run(
    root: Path,
    s: Services,
    *,
    interval: float,
    once: bool = False,
    wait: int = 50,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """La boucle. `once` : un seul passage, sans attendre (pour essayer)."""
    state = nouveautes.load(root)
    last_check = None
    while True:
        try:
            # D'abord les likes et les messages en souffrance : au démarrage,
            # rien n'attend la fin de l'écoute de Telegram.
            favorites = checked = False
            if last_check is None or clock() - last_check >= interval:
                favorites, checked = check(state, s), True
                last_check = clock()
                state.checked_at = time.strftime("%Y-%m-%d %H:%M:%S")
                state.error = None
                nouveautes.save(root, state)
            # À chaque tour, pas seulement quand un like arrive : un message
            # que Telegram n'a pas pris (réseau coupé) repart dès que possible.
            send_unsent(state, s)
            nouveautes.save(root, state)
            finish(state, s, favorites, checked)
            nouveautes.save(root, state)
            # Puis l'écoute : les boutons touchés répondent tout de suite.
            if handle(state, s, s.bot.updates(state.offset, wait=0 if once else wait)):
                nouveautes.save(root, state)
                finish(state, s, False, checked=False)
            nouveautes.save(root, state)
        except Exception as exc:  # noqa: BLE001 - la veille ne doit jamais mourir
            if once:
                raise
            s.log(f"Erreur : {exc}. Nouvel essai dans une minute.")
            state.error = f"{time.strftime('%Y-%m-%d %H:%M:%S')} : {exc}"
            try:
                nouveautes.save(root, state)
            except OSError:
                pass
            sleep(60)
        if once:
            return
