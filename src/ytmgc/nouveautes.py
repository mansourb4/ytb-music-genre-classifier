"""Nouveaux likes : proposer où ranger chacun, et retenir la réponse.

`ytmgc veille` tourne en fond sur le Mac. Toutes les quelques minutes, il
relit le haut de « Titres likés » ; chaque titre nouveau, pas encore rangé,
est analysé, puis proposé sur Telegram : son genre, la playlist où les règles
le mettraient, quelques voisines, et de quoi en choisir une autre. Tant que tu
n'as pas répondu, le titre est tenu hors de toute playlist (« (aucune) ») :
rien n'est rangé ni téléchargé sans toi. Ta réponse devient un déplacement
(data/placements.txt), comme ceux faits dans Apple Music.

Ce module ne parle à personne : il tient l'état, compose les messages et
interprète les boutons. La commande fait les appels.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from ytmgc.models import Classification, Track
from ytmgc.telegram import Button

STATE_FILE = ".nouveautes.json"
NOWHERE = "(aucune)"
#: Réponse « Ignorer pour l'instant » : ni rangé, ni écarté pour de bon.
IGNORE = "(ignorer)"
#: Voisines proposées en plus de la playlist retenue par les règles.
NEIGHBOURS = 3


@dataclass(slots=True)
class Pending:
    video_id: str
    artist: str
    title: str
    #: Texte du message (sans les boutons), pour le reprendre à chaque étape.
    text: str
    proposal: str | None = None
    message_id: int | None = None
    #: Playlist choisie, en attente de téléchargement et d'Apple Music.
    chosen: str | None = None
    #: Passages de la chaîne sans que le titre arrive dans Apple Music.
    attempts: int = 0
    #: Durée du titre (secondes), pour prendre l'extrait au bon endroit.
    duration: int | None = None
    #: L'extrait audio est parti (ou a été tenté) : pas de second envoi.
    preview_sent: bool = False


@dataclass(slots=True)
class State:
    #: Likes déjà vus (traités, proposés, ou présents au premier passage).
    seen: set[str] = field(default_factory=set)
    pending: dict[str, Pending] = field(default_factory=dict)
    next_id: int = 1
    #: Prochain événement Telegram à lire.
    offset: int | None = None
    #: Noms des playlists du plan, dans l'ordre auquel renvoient les boutons.
    names: list[str] = field(default_factory=list)
    #: Dernière relecture des likes réussie, et dernière erreur (diagnostic).
    checked_at: str | None = None
    error: str | None = None
    #: Propositions ignorées pour l'instant : identifiant YouTube → titre.
    ignored: dict[str, str] = field(default_factory=dict)
    #: Relecture demandée (« /verifier ») ; nouveaux likes du dernier passage.
    #: Ni l'un ni l'autre n'est enregistré.
    check_now: bool = False
    last_found: int = 0
    #: Ce que le dernier passage a fait de chaque nouveau like (pour « /verifier »).
    last_report: list[str] = field(default_factory=list)

    @property
    def started(self) -> bool:
        return bool(self.seen)

    def add(self, pending: Pending) -> str:
        key = str(self.next_id)
        self.next_id += 1
        self.pending[key] = pending
        return key


def load(root: Path) -> State:
    path = root / STATE_FILE
    if not path.exists():
        return State()
    data = json.loads(path.read_text(encoding="utf-8"))
    return State(
        seen=set(data.get("vus") or ()),
        pending={k: Pending(**v) for k, v in (data.get("en_attente") or {}).items()},
        next_id=int(data.get("suivant") or 1),
        offset=data.get("offset"),
        names=list(data.get("playlists") or ()),
        checked_at=data.get("relu_le"),
        error=data.get("erreur"),
        ignored=dict(data.get("ignores") or {}),
    )


def save(root: Path, state: State) -> None:
    data = {
        "vus": sorted(state.seen),
        "en_attente": {k: {f: getattr(p, f) for f in Pending.__slots__}
                       for k, p in state.pending.items()},
        "suivant": state.next_id,
        "offset": state.offset,
        "playlists": state.names,
        "relu_le": state.checked_at,
        "erreur": state.error,
        "ignores": state.ignored,
    }
    path = root / STATE_FILE
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


# ------------------------------------------------------------- proposition

def family(name: str) -> str:
    return name.split(" · ", 1)[0] if " · " in name else name


def families(names: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(family(n) for n in names))


def neighbours(proposal: str | None, names: list[str], count: int = NEIGHBOURS) -> list[str]:
    """Les playlists de la même famille que la proposition, dans l'ordre du plan."""
    if proposal is None:
        return []
    return [n for n in names if family(n) == family(proposal) and n != proposal][:count]


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def describe(track: Track, classification: Classification | None, proposal: str | None,
             reason: str | None = None) -> str:
    lines = [f"🎵 <b>{escape(track.label())}</b>"]
    if track.album:
        lines.append(f"Album : {escape(track.album)}")
    if classification is not None:
        kind = " · ".join(classification.genres[:2] + classification.styles[:3])
        if classification.year:
            kind = f"{kind} ({classification.year})" if kind else str(classification.year)
        if kind:
            lines.append(f"Genre : {escape(kind)}")
        elif classification.tags:
            lines.append(f"Tags : {escape(', '.join(classification.tags[:5]))}")
    if proposal:
        why = f" ({escape(reason)})" if reason else ""
        lines.append(f"Proposé : <b>{escape(proposal)}</b>{why}")
    else:
        lines.append("Les règles ne le rangent nulle part : choisis une famille.")
    return "\n".join(lines)


def _index(state: State, name: str) -> int:
    if name not in state.names:
        state.names.append(name)
    return state.names.index(name)


def first_buttons(state: State, key: str) -> list[list[Button]]:
    """Proposition, voisines, autre famille, ne pas ranger."""
    pending = state.pending[key]
    rows: list[list[Button]] = []
    if pending.proposal:
        rows.append([(f"✅ {pending.proposal}", f"c:{key}:{_index(state, pending.proposal)}")])
        for name in neighbours(pending.proposal, state.names):
            rows.append([(name, f"c:{key}:{_index(state, name)}")])
        rows.append([("Autre famille…", f"f:{key}"), ("Ne pas ranger", f"x:{key}")])
    else:
        rows += family_buttons(state, key)
    rows.append([("⏸ Ignorer pour l'instant", f"i:{key}")])
    return rows


def family_buttons(state: State, key: str) -> list[list[Button]]:
    names = families(state.names)
    buttons = [(name, f"g:{key}:{i}") for i, name in enumerate(names)]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    back = [("↩︎ Retour", f"b:{key}")] if state.pending[key].proposal else []
    return rows + [back + [("Ne pas ranger", f"x:{key}")]]


def playlist_buttons(state: State, key: str, family_index: int) -> list[list[Button]]:
    chosen = families(state.names)[family_index]
    buttons = [(n.split(" · ", 1)[-1], f"c:{key}:{_index(state, n)}")
               for n in state.names if family(n) == chosen]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return rows + [[("↩︎ Familles", f"f:{key}")]]


@dataclass(slots=True)
class Answer:
    """Ce qu'un bouton demande : changer les boutons, ou trancher."""

    key: str
    rows: list[list[Button]] | None = None
    #: Playlist choisie, ou « (aucune) ».
    decision: str | None = None


def interpret(state: State, data: str) -> Answer | None:
    """Données d'un bouton → réponse ; None si le titre n'attend plus rien."""
    kind, _, rest = data.partition(":")
    key, _, arg = rest.partition(":")
    if key not in state.pending or state.pending[key].chosen is not None:
        return None
    try:
        if kind == "c":
            return Answer(key, decision=state.names[int(arg)])
        if kind == "x":
            return Answer(key, decision=NOWHERE)
        if kind == "i":
            return Answer(key, decision=IGNORE)
        if kind == "f":
            return Answer(key, rows=family_buttons(state, key))
        if kind == "g":
            return Answer(key, rows=playlist_buttons(state, key, int(arg)))
        if kind == "b":
            return Answer(key, rows=first_buttons(state, key))
    except (ValueError, IndexError):
        return None
    return None


def proposal_for(video_id: str, plans: Mapping[str, Iterable[str]]) -> str | None:
    """La playlist où les règles mettent ce titre (`plans` : nom → titres)."""
    for name, ids in plans.items():
        if video_id in ids:
            return name
    return None


def _words(name: str) -> frozenset[str]:
    return frozenset(name.casefold().replace("&", " ").split()) - {"the", "and", "et"}


def same_artist(a: str, b: str) -> bool:
    """Même artiste, ou l'un contenu dans l'autre : « The Miles Davis Quintet »
    est Miles Davis. Un nom d'un seul mot (« Damso ») doit être identique."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return wa == wb or (len(wb) >= 2 and wb <= wa) or (len(wa) >= 2 and wa <= wb)


def guess(
    track: Track,
    classification: Classification | None,
    plans: Mapping[str, Iterable[str]],
    tracks: Mapping[str, Track],
    classes: Mapping[str, Classification],
) -> tuple[str | None, str | None]:
    """Une playlist pour un titre que les règles ne rangent nulle part.

    Dans l'ordre : là où sont la plupart des titres du même artiste ; sinon là
    où sont le plus de titres aux mêmes styles ; sinon aux mêmes genres. Rend
    (playlist, raison), ou (None, None) si rien ne s'en approche.
    """
    members = {name: [v for v in ids if v != track.video_id] for name, ids in plans.items()}
    artists = [a for a in track.artists if a]

    def best(score) -> str | None:
        counts = {name: sum(score(v) for v in ids) for name, ids in members.items()}
        name = max(counts, key=lambda n: counts[n], default=None)
        return name if name is not None and counts[name] > 0 else None

    if artists:
        name = best(lambda v: v in tracks and any(
            same_artist(mine, theirs) for mine in artists for theirs in tracks[v].artists))
        if name:
            return name, "là où sont ses autres titres"
    if classification is not None and classification.styles:
        styles = set(classification.styles)
        name = best(lambda v: len(styles & set(classes[v].styles)) if v in classes else 0)
        if name:
            return name, "styles proches"
    if classification is not None and classification.genres:
        genres = set(classification.genres)
        name = best(lambda v: v in classes and bool(genres & set(classes[v].genres)))
        if name:
            return name, "même genre"
    return None, None


#: Durée de l'extrait audio envoyé avec chaque proposition.
PREVIEW_SECONDS = 30


def preview_window(duration: int | None) -> tuple[int, int]:
    """(début, fin) de l'extrait : vers le tiers du morceau, là où il a démarré."""
    if not duration:
        return 45, 45 + PREVIEW_SECONDS
    if duration <= PREVIEW_SECONDS + 10:
        return 0, duration
    start = min(int(duration * 0.35), duration - PREVIEW_SECONDS)
    return start, start + PREVIEW_SECONDS
