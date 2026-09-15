"""Verdicts du modèle, tenus dans un fichier texte lisible et modifiable.

C'est le seul résultat de l'outil qui se paie. Il ne doit donc ni vivre
uniquement dans une base qu'on peut effacer, ni être redemandé à chaque
analyse : il est écrit en clair, une ligne par titre, et ce fichier fait
autorité. Le relire coûte zéro.

Trois conséquences voulues :

  * l'analyse d'un titre déjà jugé ne repart jamais vers l'API ;
  * le fichier se copie d'une machine à l'autre, se versionne, se relit ;
  * une ligne corrigée à la main l'emporte sur le modèle, définitivement —
    c'est le dernier mot de l'utilisateur sur ce qu'il écoute.

Format : des colonnes séparées par des tabulations, parce qu'un titre contient
volontiers des virgules et des points-virgules, jamais de tabulation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ytmgc.matching.normalize import normalize_artist, normalize_title
from ytmgc.models import Track

#: Colonnes du fichier, dans l'ordre. L'en-tête est réécrit à chaque
#: enregistrement : le fichier doit s'expliquer sans documentation.
COLUMNS = ("artiste", "titre", "genre", "style", "ambiance", "confiance", "source", "note")

HEADER = f"""\
# Verdicts ytmgc — ce que le modèle (ou toi) dit de chaque titre.
#
# Une ligne par titre, colonnes séparées par des tabulations :
#   {'  |  '.join(COLUMNS)}
#
# Ce fichier fait autorité : un titre qui y figure n'est plus jamais envoyé à
# l'API, et sa ligne l'emporte sur Discogs comme sur Last.fm. Corrige une
# ligne à la main et la correction tient — mets alors "manuel" en source pour
# t'en souvenir. Supprime une ligne pour que le titre soit redemandé.
#
# confiance : de 0 à 1, l'assurance du modèle sur ce couple genre/style.
# source    : "claude" (jugé par le modèle) ou "manuel" (jugé par toi).
"""

#: Marque d'une ligne écrite à la main plutôt que par le modèle.
MANUAL = "manuel"
MODEL = "claude"


def entry_key(artist: str, title: str) -> str:
    """Clé d'un titre : artiste et titre normalisés, jamais l'album.

    La même normalisation que pour Last.fm, et pour la même raison : le fichier
    doit rester valable après un changement de compte, un réencodage ou une
    pochette différente. Deux éditions d'un même morceau partagent leur verdict.
    """
    return f"{normalize_artist(artist)}::{normalize_title(title)}"


def track_key(track: Track) -> str:
    return entry_key(track.artist, track.title)


@dataclass(frozen=True, slots=True)
class Verdict:
    """Ce qu'on sait d'un titre, indépendamment de toute base de données."""

    artist: str
    title: str
    genre: str
    style: str
    mood: str
    confidence: float = 0.0
    source: str = MODEL
    note: str = ""

    @property
    def key(self) -> str:
        return entry_key(self.artist, self.title)

    def line(self) -> str:
        fields = (
            self.artist, self.title, self.genre, self.style, self.mood,
            f"{self.confidence:.2f}", self.source, self.note,
        )
        # Une tabulation dans un champ casserait silencieusement la colonne
        # suivante ; un espace ne change rien à la lecture.
        return "\t".join(field.replace("\t", " ").replace("\n", " ") for field in fields)


def parse_line(line: str) -> Verdict | None:
    """Lit une ligne, ou None si elle n'en est pas une.

    Tolérante par construction : le fichier est fait pour être édité à la main,
    et une ligne bancale ne doit pas rendre les mille autres inutilisables.
    """
    stripped = line.strip("\n")
    if not stripped.strip() or stripped.lstrip().startswith("#"):
        return None

    parts = stripped.split("\t")
    if len(parts) < 5:
        return None
    parts += [""] * (len(COLUMNS) - len(parts))
    artist, title, genre, style, mood, confidence, source, note = parts[: len(COLUMNS)]
    if not artist.strip() or not title.strip():
        return None

    try:
        weight = float(confidence)
    except ValueError:
        weight = 0.0

    return Verdict(
        artist=artist.strip(), title=title.strip(), genre=genre.strip(),
        style=style.strip(), mood=mood.strip(), confidence=weight,
        source=source.strip() or MODEL, note=note.strip(),
    )


class VerdictBook:
    """L'ensemble des verdicts connus, indexé par (artiste, titre)."""

    def __init__(self, verdicts: list[Verdict] | None = None) -> None:
        self._by_key: dict[str, Verdict] = {}
        for verdict in verdicts or []:
            self._by_key[verdict.key] = verdict

    def __len__(self) -> int:
        return len(self._by_key)

    def __contains__(self, key: object) -> bool:
        return key in self._by_key

    def __iter__(self):
        return iter(self._by_key.values())

    def get(self, key: str) -> Verdict | None:
        return self._by_key.get(key)

    def for_track(self, track: Track) -> Verdict | None:
        return self._by_key.get(track_key(track))

    def add(self, verdict: Verdict) -> None:
        """Ajoute un verdict, sauf à écraser une décision humaine.

        Une ligne corrigée à la main est le dernier mot : une nouvelle passe du
        modèle ne doit pas la balayer sans que l'utilisateur ait rien demandé.
        """
        existing = self._by_key.get(verdict.key)
        if existing is not None and existing.source == MANUAL and verdict.source != MANUAL:
            return
        self._by_key[verdict.key] = verdict

    def missing(self, tracks: list[Track]) -> list[Track]:
        """Les titres dont on n'a pas encore de verdict, sans doublon.

        Deux enregistrements du même morceau ne sont jugés qu'une fois : c'est
        autant de moins à payer.
        """
        seen: set[str] = set()
        pending: list[Track] = []
        for track in tracks:
            key = track_key(track)
            if key in self._by_key or key in seen:
                continue
            seen.add(key)
            pending.append(track)
        return pending

    def lines(self) -> list[str]:
        """Le contenu du fichier, trié : deux enregistrements successifs se comparent."""
        return [
            verdict.line()
            for verdict in sorted(
                self._by_key.values(),
                key=lambda v: (normalize_artist(v.artist), normalize_title(v.title)),
            )
        ]


def load(path: str | Path) -> VerdictBook:
    """Lit le fichier. Un fichier absent donne un recueil vide, pas une erreur."""
    file = Path(path)
    if not file.exists():
        return VerdictBook()
    verdicts = [
        verdict
        for verdict in (parse_line(line) for line in file.read_text(encoding="utf-8").splitlines())
        if verdict is not None
    ]
    return VerdictBook(verdicts)


def save(book: VerdictBook, path: str | Path) -> int:
    """Écrit le fichier entier, en passant par un fichier temporaire.

    Une interruption au milieu de l'écriture détruirait des verdicts payés :
    le renommage final est atomique, donc l'ancien fichier tient jusqu'au bout.
    """
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    body = HEADER + "\n".join(book.lines()) + "\n"
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(file)
    return len(book)


def manual(verdict: Verdict) -> Verdict:
    """Le même verdict, marqué comme décidé par un humain."""
    return replace(verdict, source=MANUAL)
