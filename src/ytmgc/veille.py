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

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ytmgc import nouveautes, placements
from ytmgc.download import song_key
from ytmgc.models import Classification, Track
from ytmgc.nouveautes import IGNORE, NOWHERE, Pending, State, escape
from ytmgc.telegram import Bot

#: Titres relus en haut de « Titres likés » à chaque passage.
LOOK = 200
#: Essais de la chaîne pour un titre choisi, avant d'abandonner.
MAX_ATTEMPTS = 3
#: Le menu « / » du bot dans Telegram.
COMMANDS = [
    {"command": "verifier", "description": "Regarder tout de suite les nouveaux likes"},
    {"command": "proposer", "description": "Revoir la proposition d'un like (ex. /proposer miles)"},
    {"command": "ignores", "description": "Les propositions ignorées pour l'instant"},
    {"command": "etat", "description": "Ce qui attend ta réponse"},
]


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
    #: Extrait audio d'un titre en attente (fichier temporaire), ou None.
    preview: Callable[[Pending], Path | None] | None = None


def _track(pending: Pending) -> Track:
    return Track(pending.video_id, pending.title, (pending.artist,) if pending.artist else ())


def check(state: State, s: Services) -> bool:
    """Relit les likes ; propose les nouveaux. Rend True s'il faut des favoris."""
    if not state.started:
        state.seen = {t.video_id for t in s.liked(None)}
        s.log(f"Premier passage : {len(state.seen)} like(s) existants, laissés tels quels.")
        return False
    guess_missing(state, s)
    new = [t for t in s.liked(LOOK) if t.video_id not in state.seen]
    state.last_found = len(new)
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
        propose(state, s, fresh)
    state.seen.update(t.video_id for t in new)
    return favorites


def propose(state: State, s: Services, tracks: list[Track]) -> None:
    """Analyse ces titres, les tient hors playlist et prépare leur proposition
    (le message part au tour suivant de la boucle)."""
    s.save_tracks(tracks)
    s.classify(tracks)
    # Un titre déjà tenu hors playlist (« Ne pas ranger ») : les règles ne le
    # rangent plus ; on lève ce choix le temps de calculer la proposition.
    book = placements.load(s.placements_file)
    if any([book.remove(track) for track in tracks]):  # tous, pas seulement le premier
        placements.save(book, s.placements_file)
    plans = s.plans()
    state.names = list(plans)
    classes = {c.video_id: c for c in s.classifications()}
    known = {t.video_id: t for t in s.tracks()}
    for track in tracks:
        proposal, reason = nouveautes.proposal_for(track.video_id, plans), None
        if proposal is None:
            proposal, reason = nouveautes.guess(track, classes.get(track.video_id), plans,
                                                known, classes)
        # Tenu hors playlist jusqu'à ta réponse : rien ne se range sans toi.
        book.put(track, NOWHERE)
        state.add(Pending(track.video_id, track.artist, track.title,
                          nouveautes.describe(track, classes.get(track.video_id), proposal,
                                              reason),
                          proposal, duration=track.duration_s))
        s.log(f"Proposé : {track.label()} → {proposal or 'aucune'}")
    placements.save(book, s.placements_file)


def repropose(state: State, s: Services, query: str) -> None:
    """« /proposer miles » : refaire la proposition d'un like récent, pour
    revenir sur un « Ne pas ranger » ou ranger ailleurs."""
    wanted = " ".join(query.casefold().split())
    if not wanted:
        s.bot.send(s.chat, "Écris par exemple « /proposer miles davis » : je cherche parmi "
                           f"tes {LOOK} derniers likes.")
        return
    waiting = {p.video_id for p in state.pending.values()}
    found = [t for t in s.liked(LOOK) if wanted in " ".join(t.label().casefold().split())]
    if not found:
        s.bot.send(s.chat, f"Aucun de tes {LOOK} derniers likes ne contient « "
                           f"{escape(query.strip())} ».")
        return
    fresh = [t for t in found[:3] if t.video_id not in waiting]
    if not fresh:
        s.bot.send(s.chat, "Déjà proposé plus haut : il attend ta réponse.")
        return
    for track in fresh:
        state.ignored.pop(track.video_id, None)
    propose(state, s, fresh)
    send_unsent(state, s)


def list_ignored(state: State, s: Services) -> None:
    if not state.ignored:
        s.bot.send(s.chat, "Aucune proposition ignorée.")
        return
    lines = [f"{len(state.ignored)} proposition(s) ignorée(s) pour l'instant :"]
    lines += [f"• {escape(label)}" for label in list(state.ignored.values())[:30]]
    lines.append("Pour en revoir une : « /proposer » suivi d'un mot du titre ou de l'artiste.")
    s.bot.send(s.chat, "\n".join(lines))


def guess_missing(state: State, s: Services) -> None:
    """Une proposition pour les titres en attente qui n'en avaient pas (envoyés
    avant que la veille sache deviner) : le message est mis à jour."""
    waiting = {k: p for k, p in state.pending.items() if p.proposal is None and not p.chosen}
    if not waiting:
        return
    plans = s.plans()
    state.names = list(plans)
    classes = {c.video_id: c for c in s.classifications()}
    known = {t.video_id: t for t in s.tracks()}
    for key, pending in waiting.items():
        track = known.get(pending.video_id) or _track(pending)
        proposal, reason = nouveautes.guess(track, classes.get(pending.video_id), plans,
                                            known, classes)
        if proposal is None:
            continue
        pending.proposal = proposal
        pending.text = nouveautes.describe(track, classes.get(pending.video_id), proposal, reason)
        # Un message modifié ne sonne pas : l'ancien est désactivé, et la
        # proposition repart dans un nouveau message, qui, lui, se voit.
        if pending.message_id is not None:
            s.bot.edit(s.chat, pending.message_id,
                       f"🎵 {nouveautes.escape(track.label())}\n↪︎ Nouvelle proposition plus bas.")
            pending.message_id = None
        s.log(f"Proposition ajoutée : {track.label()} → {proposal}")


def send_unsent(state: State, s: Services) -> None:
    """Envoie les propositions pas encore parties (Telegram injoignable avant)."""
    for key, pending in state.pending.items():
        if pending.message_id is None:
            audio = send_preview(pending, s)
            pending.message_id = s.bot.send(s.chat, pending.text,
                                            nouveautes.first_buttons(state, key), reply_to=audio)
            s.log(f"Proposition envoyée sur Telegram : {_track(pending).label()}")


def send_preview(pending: Pending, s: Services) -> int | None:
    """Envoie l'extrait audio du titre, une seule fois. Un extrait manqué
    n'empêche jamais la proposition de partir."""
    if pending.preview_sent or s.preview is None:
        return None
    pending.preview_sent = True
    path = None
    try:
        path = s.preview(pending)
        if path is None:
            return None
        return s.bot.send_audio(s.chat, path, title=pending.title, performer=pending.artist)
    except Exception as exc:  # noqa: BLE001 - l'extrait n'est qu'un plus
        s.log(f"Extrait audio impossible pour {_track(pending).label()} : {exc}")
        return None
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
            if path.parent.name.startswith("ytmgc-extrait-"):
                shutil.rmtree(path.parent, ignore_errors=True)


def handle(state: State, s: Services, updates: Iterable[dict]) -> int:
    """Traite les boutons touchés. Rend le nombre de choix faits."""
    decided = 0
    for update in updates:
        state.offset = int(update["update_id"]) + 1
        message = update.get("message")
        if message and str(message.get("chat", {}).get("id")) == s.chat:
            command, _, rest = (message.get("text") or "").strip().partition(" ")
            command = command.casefold().split("@")[0]
            if command in {"/proposer", "/revoir"}:
                repropose(state, s, rest)
            elif command in {"/verifier", "/vérifier"}:
                state.check_now = True
                s.bot.send(s.chat, "🔎 Je regarde tes likes…")
            elif command in {"/ignores", "/ignorés"}:
                list_ignored(state, s)
            else:
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
        if answer.decision == IGNORE:
            # Ni rangé ni écarté : il reste tenu hors playlist, et revient
            # avec « /proposer ».
            s.bot.answer(query["id"], "Mis de côté.")
            s.bot.edit(s.chat, pending.message_id, pending.text + "\n\n⏸ Ignoré pour l'instant. "
                       "Pour le revoir : « /proposer " + escape(pending.title[:30]) + " ».")
            state.ignored[pending.video_id] = _track(pending).label()
            del state.pending[answer.key]
            s.log(f"Ignoré pour l'instant : {_track(pending).label()}")
            continue
        book = placements.load(s.placements_file)
        book.put(_track(pending), answer.decision)
        placements.save(book, s.placements_file)
        s.bot.answer(query["id"], "Noté.")
        if answer.decision == NOWHERE:
            s.bot.edit(s.chat, pending.message_id, pending.text + "\n\n👌 Laissé hors playlists. "
                       "Pour y revenir : « /proposer " + escape(pending.title[:30]) + " ».")
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
    if state.ignored:
        lines.append(f"{len(state.ignored)} proposition(s) ignorée(s) : /ignores pour les voir.")
    lines.append("/verifier : regarder tout de suite si de nouveaux titres ont été likés.\n"
                 "/proposer miles : revoir la proposition d'un like récent.")
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
    try:
        s.bot.call("setMyCommands", commands=COMMANDS)
    except Exception:  # noqa: BLE001 - le menu n'est qu'un confort
        pass
    while True:
        try:
            # D'abord les likes et les messages en souffrance : au démarrage,
            # rien n'attend la fin de l'écoute de Telegram.
            favorites = checked = False
            asked, state.check_now = state.check_now, False
            if asked or last_check is None or clock() - last_check >= interval:
                favorites, checked = check(state, s), True
                if asked:
                    found = state.last_found
                    s.bot.send(s.chat, "Rien de nouveau dans tes likes." if not found else
                               f"{found} nouveau(x) like(s) : les propositions suivent.")
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
