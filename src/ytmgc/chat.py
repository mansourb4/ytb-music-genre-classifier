"""Passe gratuite : faire juger les titres dans une conversation Claude.ai.

Les mêmes instructions que la passe par l'API, le même vocabulaire, le même
fichier de verdicts — seul le transport change. L'outil prépare un *paquet*
(instructions, vocabulaire, une centaine de titres numérotés) que l'utilisateur
colle dans une conversation ; il recopie la réponse, et l'outil la lit.

Ce transport a un défaut que l'API n'a pas : rien n'y contraint la forme de la
réponse. Un modèle qui saute un numéro décalerait tous les suivants, et chaque
verdict décalé serait écrit sous le titre d'un autre morceau — puis tenu pour
acquis, jamais redemandé. La lecture est donc défiante :

  * le paquet porte un identifiant, que la réponse doit reprendre : une réponse
    collée face au mauvais paquet est refusée en bloc ;
  * chaque verdict reprend le titre qu'il juge, et un titre qui ne correspond
    pas à son numéro fait écarter ce verdict ;
  * genre et ambiance doivent appartenir au vocabulaire fermé — une ambiance
    inventée ouvrirait une neuvième playlist.

Ce qui est écarté n'est pas perdu : le titre reste sans verdict, et revient
de lui-même dans le paquet suivant.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ytmgc.matching.normalize import fold, song_key
from ytmgc.sources.claude import SYSTEM, Subject, build_reference
from ytmgc.taxonomy import Taxonomy
from ytmgc.verdicts import MODEL, Verdict


class ChatError(ValueError):
    """Réponse inutilisable telle quelle, avec ce qu'il faut faire."""


@dataclass(frozen=True, slots=True)
class Packet:
    """Un lot de titres à faire juger dans une conversation."""

    id: str
    subjects: list[Subject]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "subjects": [subject.to_dict() for subject in self.subjects]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Packet":
        return cls(
            id=data["id"],
            subjects=[Subject.from_dict(item) for item in data.get("subjects", [])],
        )


def packet_id(subjects: list[Subject]) -> str:
    """Identifiant tiré du contenu du paquet, pas du moment où il est préparé.

    Préparer deux fois le même paquet — parce qu'on a fermé l'onglet, perdu le
    texte — donne le même identifiant : la réponse obtenue avec la première
    copie reste importable.
    """
    keys = "\n".join(song_key(subject.artist, subject.title) for subject in subjects)
    return hashlib.sha1(keys.encode("utf-8")).hexdigest()[:8]


def make_packet(subjects: list[Subject]) -> Packet:
    return Packet(id=packet_id(subjects), subjects=list(subjects))


FORMAT = """\
FORMAT DE LA RÉPONSE

Réponds uniquement par un bloc de code JSON, sans rien avant ni après :

```json
{{"paquet": "{id}", "verdicts": [
  {{"n": 1, "titre": "titre tel que listé", "genre": "…", "style": "…", "mood": "…", "confidence": 0.9, "note": "…"}}
]}}
```

- `paquet` : reprends exactement « {id} ».
- `titre` : recopie le titre tel qu'il figure dans la liste, pour chaque numéro.
- Les {count} titres, dans l'ordre, un objet chacun.
- Si la réponse ne tient pas en un message, arrête-toi après un objet complet
  et ferme le bloc. On te dira « continue » : reprends alors au numéro suivant,
  dans un nouveau bloc complet du même format, avec le même `paquet`.
"""


def build_message(packet: Packet, taxonomy: Taxonomy) -> str:
    """Le texte à coller dans la conversation, d'un seul tenant."""
    listing = "\n".join(
        subject.line(index) for index, subject in enumerate(packet.subjects, start=1)
    )
    return "\n\n".join(
        (
            SYSTEM.strip(),
            FORMAT.format(id=packet.id, count=len(packet.subjects)).strip(),
            build_reference(taxonomy),
            f"TITRES À JUGER — paquet {packet.id} ({len(packet.subjects)} titres)\n\n{listing}",
        )
    )


def save_packet(packet: Packet, path: str | Path) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(packet.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


def load_packet(path: str | Path) -> Packet | None:
    file = Path(path)
    if not file.exists():
        return None
    try:
        return Packet.from_dict(json.loads(file.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------- lecture

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(slots=True)
class Reading:
    """Ce qu'une réponse a donné, et ce qui en a été écarté."""

    verdicts: list[Verdict] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def _blocks(text: str) -> list[Any]:
    """Les objets JSON d'une réponse collée, avec ou sans bloc de code.

    Une réponse longue arrive en plusieurs messages — « continue » — et
    l'utilisateur colle souvent le tout d'un coup : chaque bloc est lu.
    """
    candidates = [match.group(1) for match in _FENCE_RE.finditer(text)]
    if not candidates:
        start, end = text.find("{"), text.rfind("}")
        candidates = [text[start:end + 1]] if 0 <= start < end else []

    blocks: list[Any] = []
    for candidate in candidates:
        try:
            blocks.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return blocks


def _vocabulary(values: list[str]) -> dict[str, str]:
    """Correspondance tolérante vers le libellé exact : « melancolique » ->
    « Mélancolique ». La casse et les accents ne doivent pas coûter un verdict."""
    return {fold(value): value for value in values}


def _same_song(subject: Subject, echoed: str) -> bool:
    """Le titre recopié désigne-t-il bien le morceau de ce numéro ?

    Tolérant sur la forme — le modèle recopie volontiers « Teen Spirit » sans
    « (Official Video) » — mais pas sur le fond : un autre morceau, et c'est
    le signe d'un décalage de numérotation.
    """
    expected = song_key(subject.artist, subject.title).partition("::")[2]
    given = song_key(subject.artist, echoed).partition("::")[2]
    if not expected or not given:
        return False
    return expected == given or expected.startswith(given + " ") or given.startswith(expected + " ")


def read_answer(text: str, packet: Packet, taxonomy: Taxonomy) -> Reading:
    """Lit une réponse collée, en n'acceptant que ce qui est vérifiable."""
    blocks = _blocks(text)
    if not blocks:
        raise ChatError(
            "Aucun JSON lisible dans ce texte. Copie la réponse avec le bouton "
            "« Copier » du bloc de code, plutôt qu'en sélectionnant à la main."
        )

    genres = _vocabulary(taxonomy.genres())
    moods = _vocabulary(taxonomy.moods())
    reading = Reading()
    seen: set[int] = set()

    for block in blocks:
        if isinstance(block, list):
            block = {"verdicts": block}
        if not isinstance(block, dict):
            continue
        answered = str(block.get("paquet") or "").strip()
        if answered and answered != packet.id:
            raise ChatError(
                f"Cette réponse est celle du paquet {answered}, or le paquet en cours "
                f"est {packet.id}. Colle la réponse au paquet affiché, ou prépare-le à "
                "nouveau : un même lot de titres redonne toujours le même paquet."
            )

        for item in block.get("verdicts") or []:
            verdict = _read_item(item, packet, genres, moods, seen, reading.rejected)
            if verdict is not None:
                reading.verdicts.append(verdict)
    return reading


def _read_item(
    item: Any,
    packet: Packet,
    genres: dict[str, str],
    moods: dict[str, str],
    seen: set[int],
    rejected: list[str],
) -> Verdict | None:
    if not isinstance(item, dict):
        return None
    try:
        number = int(item.get("n"))
    except (TypeError, ValueError):
        rejected.append(f"objet sans numéro lisible : {str(item)[:60]}")
        return None
    if not 1 <= number <= len(packet.subjects):
        rejected.append(f"n°{number} : hors du paquet")
        return None
    if number in seen:
        return None
    subject = packet.subjects[number - 1]
    label = f"n°{number} {subject.artist} – {subject.title}"

    # Le titre recopié est exigé, pas seulement vérifié quand il est là : sans
    # lui, un décalage de numérotation passerait inaperçu.
    echoed = str(item.get("titre") or "").strip()
    if not echoed:
        rejected.append(f"{label} : titre non recopié, impossible de vérifier le numéro")
        return None
    if not _same_song(subject, echoed):
        rejected.append(f"{label} : la réponse parle de « {echoed} » (numérotation décalée ?)")
        return None
    genre = genres.get(fold(str(item.get("genre") or "")))
    if genre is None:
        rejected.append(f"{label} : genre « {item.get('genre')} » hors de la liste")
        return None
    mood = moods.get(fold(str(item.get("mood") or "")))
    if mood is None:
        rejected.append(f"{label} : ambiance « {item.get('mood')} » hors de la liste")
        return None

    try:
        confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0

    seen.add(number)
    return Verdict(
        artist=subject.artist,
        title=subject.title,
        genre=genre,
        style=str(item.get("style") or "").strip(),
        mood=mood,
        confidence=confidence,
        source=MODEL,
        note=str(item.get("note") or "").strip(),
    )


# --------------------------------------------------------- de bout en bout


@dataclass(slots=True)
class Imported:
    """Bilan d'une réponse collée."""

    accepted: int
    applied: int
    rejected: list[str]
    #: Titres du paquet encore sans verdict : la réponse était partielle, ou
    #: certains objets ont été écartés.
    left_in_packet: int
    #: Titres de la bibliothèque encore sans verdict, ce paquet compris.
    remaining: int

    def line(self) -> str:
        parts = [f"{self.accepted} verdict(s) enregistré(s)"]
        if self.applied:
            parts.append(f"{self.applied} titre(s) reclassé(s)")
        if self.rejected:
            parts.append(f"{len(self.rejected)} écarté(s)")
        return ", ".join(parts) + f". Reste {self.remaining} titre(s) à juger."


def prepare(
    repository, config, *, size: int | None = None, only_unsorted: bool = False
) -> tuple[Packet | None, int]:
    """Prépare le paquet suivant et le note sur le disque.

    Renvoie aussi le nombre total de titres encore à juger : c'est ce qui dit
    combien d'allers-retours il reste.
    """
    from ytmgc.enrich import pending_subjects
    from ytmgc.verdicts import load

    book = load(config.claude.verdicts_file)
    remaining = len(pending_subjects(repository, book, only_unsorted=only_unsorted))
    subjects = pending_subjects(
        repository, book, limit=size or config.claude.packet_size, only_unsorted=only_unsorted
    )
    if not subjects:
        return None, 0
    packet = make_packet(subjects)
    save_packet(packet, config.claude.packet_file)
    return packet, remaining


def import_answer(text: str, repository, config, taxonomy: Taxonomy) -> Imported:
    """Lit une réponse, écrit les verdicts acceptés, et range la bibliothèque.

    Le paquet n'est pas oublié après l'import : si Claude a répondu en
    plusieurs messages, la suite doit pouvoir être collée à son tour.
    """
    from ytmgc.enrich import apply_verdicts, pending_subjects
    from ytmgc.verdicts import entry_key, load, save

    packet = load_packet(config.claude.packet_file)
    if packet is None:
        raise ChatError("Aucun paquet en cours : prépare d'abord un paquet à coller dans Claude.ai.")

    reading = read_answer(text, packet, taxonomy)
    book = load(config.claude.verdicts_file)
    for verdict in reading.verdicts:
        book.add(verdict)
    if reading.verdicts:
        save(book, config.claude.verdicts_file)

    return Imported(
        accepted=len(reading.verdicts),
        applied=apply_verdicts(repository, book, taxonomy, config),
        rejected=reading.rejected,
        left_in_packet=sum(
            1 for subject in packet.subjects
            if book.get(entry_key(subject.artist, subject.title)) is None
        ),
        remaining=len(pending_subjects(repository, book)),
    )
