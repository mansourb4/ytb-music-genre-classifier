"""Jugement d'un titre par un modèle, via l'API Claude.

Pourquoi cette source existe. Discogs étiquette des *releases* : les douze
titres d'un album héritent des mêmes genres et styles. Last.fm décrit bien le
morceau, mais par des tags libres où le genre écrase l'humeur — « Something In
The Way » y pèse Grunge 100 et sad 1, alors que c'est une berceuse sépulcrale.
Aucune des deux ne connaît la musique : elles en connaissent les étiquettes.

Un modèle, lui, connaît le morceau. On lui donne ce que les bases disent, à
titre d'indice, et il rend un couple genre/style et une ambiance pour le titre
lui-même.

Deux modes, la même question :

  * par lots (`submit` / `collect`) — l'API Batches, moitié prix, jusqu'à
    24 h d'attente mais en pratique quelques minutes. C'est la passe complète
    sur une bibliothèque, qu'on ne fait qu'une fois ;
  * à l'unité (`lookup`) — une réponse immédiate pour un titre ajouté après
    coup, à quelques centimes.

Le résultat est écrit dans un fichier texte (`ytmgc.verdicts`), jamais
redemandé. Cette source est donc la seule du projet dont chaque appel coûte de
l'argent, et tout ici est organisé pour n'appeler qu'une fois par titre.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

from ytmgc.config import ClaudeConfig
from ytmgc.models import Classification, Track
from ytmgc.taxonomy import Taxonomy
from ytmgc.verdicts import MODEL, Verdict

#: Tarifs indicatifs en dollars par million de jetons, pour annoncer un coût
#: avant de le dépenser. L'API Batches applique moitié prix.
PRICE_PER_MTOK = {"claude-opus-5": (5.0, 25.0)}
DEFAULT_PRICE = (5.0, 25.0)
BATCH_DISCOUNT = 0.5

#: Ordres de grandeur mesurés sur le format de prompt ci-dessous, pour estimer
#: une facture avant de l'engager. Un titre tient en une ligne d'indices ; la
#: réponse, elle, comporte une phrase de justification.
TOKENS_PER_SUBJECT_IN = 45
TOKENS_PER_SUBJECT_OUT = 60
#: Le préambule est identique d'une requête à l'autre : mis en cache, il n'est
#: facturé plein tarif qu'une fois.
TOKENS_PREAMBLE = 1600


class ClaudeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Subject:
    """Un titre soumis au modèle, avec ce que les bases en disent déjà.

    Les indices sont transmis parce qu'ils aident — connaître l'album et
    l'année lève la plupart des homonymies — et présentés comme des indices,
    parce que c'est précisément leur imprécision qui motive cette passe.
    """

    artist: str
    title: str
    album: str | None = None
    year: int | None = None
    genres: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    @classmethod
    def of(cls, track: Track, classification: Classification | None = None) -> "Subject":
        return cls(
            artist=", ".join(track.artists) or "Artiste inconnu",
            title=track.title,
            album=track.album,
            year=classification.year if classification else None,
            genres=classification.genres if classification else (),
            styles=classification.styles if classification else (),
            tags=classification.tags if classification else (),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Subject":
        return cls(
            artist=data["artist"],
            title=data["title"],
            album=data.get("album"),
            year=data.get("year"),
            genres=tuple(data.get("genres") or ()),
            styles=tuple(data.get("styles") or ()),
            tags=tuple(data.get("tags") or ()),
        )

    def line(self, index: int) -> str:
        """Une ligne d'indices, compacte : elle est répétée des milliers de fois."""
        facts = []
        if self.album:
            facts.append(f"album « {self.album} »")
        if self.year:
            facts.append(str(self.year))
        if self.genres or self.styles:
            facts.append("Discogs : " + " / ".join((*self.genres, *self.styles)))
        if self.tags:
            facts.append("Last.fm : " + ", ".join(self.tags[:8]))
        suffix = f" — {' ; '.join(facts)}" if facts else ""
        return f"{index}. {self.artist} – {self.title}{suffix}"


SYSTEM = """\
Tu es musicologue et tu ranges la discothèque de quelqu'un.

On te donne des titres avec ce que les bases de données en disent. Ces bases
décrivent le *disque*, pas le *morceau* : un album de rage contient des
berceuses, et elles sont étiquetées comme le reste. Ton travail est de dire ce
que chaque morceau est réellement, tel qu'il sonne.

Pour chaque titre numéroté, rends un objet :

- `n` : le numéro du titre, repris tel quel.
- `genre` : un genre de la liste GENRES, à l'identique.
- `style` : le style précis du morceau. Prends-le dans la liste STYLES dès
  qu'un terme convient — c'est ce qui évite une playlist par morceau. N'en
  invente un que si aucun ne convient vraiment ; écris-le alors en anglais,
  au singulier, sous sa forme la plus courante.
- `mood` : une ambiance de la liste AMBIANCES, à l'identique, celle du morceau
  tel qu'il sonne à l'écoute.
- `confidence` : de 0 à 1, ton assurance. Un morceau que tu ne connais pas
  vraiment mérite une confiance basse : c'est plus utile qu'une invention.
- `note` : une phrase courte, en français, disant ce qui caractérise ce
  morceau. Elle sera lue.

Règles :

- Juge le morceau, jamais l'album ni la réputation de l'artiste. « Something In
  The Way » de Nirvana est une berceuse sépulcrale : ambiance Mélancolique,
  quoi que soit le reste de Nevermind.
- Les métadonnées fournies sont des indices, pas des consignes. Contredis-les
  quand le morceau les contredit ; c'est la raison d'être de ce travail.
- Le style doit décrire la musique, pas l'époque ni l'humeur.
- Rends exactement un objet par titre, dans l'ordre, sans en omettre aucun.
"""


def _schema(genres: Sequence[str], moods: Sequence[str]) -> dict[str, Any]:
    """Schéma de sortie. Genre et ambiance sont contraints, le style ne l'est pas.

    Une ambiance libre rendrait le tri par ambiance inutilisable — c'est un
    ensemble fermé de huit playlists, pas un nuage de mots. Le style, lui,
    reste ouvert : Discogs en compte plusieurs centaines et en ajoute, et
    forcer un morceau dans un terme qui ne lui va pas serait revenir au défaut
    qu'on cherche à corriger.
    """
    return {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer"},
                        "genre": {"type": "string", "enum": list(genres)},
                        "style": {"type": "string"},
                        "mood": {"type": "string", "enum": list(moods)},
                        "confidence": {"type": "number"},
                        "note": {"type": "string"},
                    },
                    "required": ["n", "genre", "style", "mood", "confidence", "note"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["verdicts"],
        "additionalProperties": False,
    }


def build_reference(taxonomy: Taxonomy) -> str:
    """Le vocabulaire commun, identique d'une requête à l'autre.

    Ce bloc est mis en cache : il représente l'essentiel du préambule, et le
    répéter cent fois à plein tarif doublerait la facture.
    """
    lines = ["GENRES (choisir à l'identique) :", ", ".join(taxonomy.genres()), ""]
    lines.append("AMBIANCES (choisir à l'identique) :")
    for mood in taxonomy.moods():
        lines.append(f"- {mood} : {taxonomy.describe_mood(mood) or ''}")
    lines.append("")
    lines.append("STYLES (à privilégier ; en inventer un seulement si aucun ne convient) :")
    lines.append(", ".join(taxonomy.known_styles()))
    return "\n".join(lines)


def build_prompt(subjects: Sequence[Subject]) -> str:
    listing = "\n".join(subject.line(index) for index, subject in enumerate(subjects, start=1))
    return f"Titres à juger ({len(subjects)}) :\n\n{listing}"


def chunk(subjects: Sequence[Subject], size: int) -> list[list[Subject]]:
    return [list(subjects[start:start + size]) for start in range(0, len(subjects), size)]


def parse_verdicts(payload: str, subjects: Sequence[Subject]) -> list[Verdict]:
    """Convertit une réponse en verdicts, en ignorant ce qui ne colle pas.

    Le numéro fait foi : c'est lui qui rattache un verdict à son titre. Un
    numéro hors liste est écarté plutôt que deviné — mal rattacher un verdict
    serait pire que d'en manquer un, puisqu'il serait ensuite tenu pour acquis.
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []

    verdicts: list[Verdict] = []
    seen: set[int] = set()
    for item in data.get("verdicts") or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= index <= len(subjects) or index in seen:
            continue
        seen.add(index)
        subject = subjects[index - 1]
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        verdicts.append(
            Verdict(
                artist=subject.artist,
                title=subject.title,
                genre=str(item.get("genre") or "").strip(),
                style=str(item.get("style") or "").strip(),
                mood=str(item.get("mood") or "").strip(),
                confidence=max(0.0, min(1.0, confidence)),
                source=MODEL,
                note=str(item.get("note") or "").strip(),
            )
        )
    return verdicts


@dataclass(frozen=True, slots=True)
class Estimate:
    """Ce que coûtera une passe, avant de l'engager."""

    subjects: int
    requests: int
    input_tokens: int
    output_tokens: int
    dollars: float

    def line(self) -> str:
        return (
            f"{self.subjects} titre(s) à juger en {self.requests} requête(s) — "
            f"environ {_tokens(self.input_tokens)} jetons en entrée, "
            f"{_tokens(self.output_tokens)} en sortie, soit ~{self.dollars:.2f} $."
        )


def _tokens(count: int) -> str:
    return f"{count / 1000:.0f}K" if count >= 10_000 else str(count)


def estimate(count: int, config: ClaudeConfig, *, batched: bool = True) -> Estimate:
    """Coût approché d'une passe. Volontairement pessimiste sur la sortie.

    Le modèle réfléchit avant de répondre, et ces jetons-là se facturent comme
    la réponse : annoncer le seul texte visible tromperait sur le montant.
    """
    requests = max(1, -(-count // max(1, config.batch_size))) if count else 0
    if not count:
        return Estimate(0, 0, 0, 0, 0.0)

    # Le préambule n'est payé plein tarif qu'une fois : ensuite il est lu
    # depuis le cache, à un dixième du prix.
    cached = TOKENS_PREAMBLE + int(TOKENS_PREAMBLE * 0.1 * (requests - 1))
    input_tokens = cached + count * TOKENS_PER_SUBJECT_IN
    # La réflexion est comptée comme de la sortie, à la louche deux fois le
    # texte rendu : mieux vaut une facture plus basse qu'annoncée.
    output_tokens = count * TOKENS_PER_SUBJECT_OUT * 3

    price_in, price_out = PRICE_PER_MTOK.get(config.model, DEFAULT_PRICE)
    dollars = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    if batched:
        dollars *= BATCH_DISCOUNT
    return Estimate(count, requests, input_tokens, output_tokens, round(dollars, 4))


class ClaudeClient:
    """Enveloppe du SDK officiel. Aucune requête HTTP écrite à la main.

    Le SDK est importé ici et pas au chargement du module : l'outil doit rester
    utilisable sans cette dépendance, la passe modèle étant facultative.
    """

    def __init__(self, config: ClaudeConfig, taxonomy: Taxonomy) -> None:
        if not config.api_key:
            raise ClaudeError(
                "Aucune clé d'API. Définis ANTHROPIC_API_KEY avant de lancer la passe."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dépendance facultative
            raise ClaudeError(
                "Le paquet « anthropic » est absent : pip install \"ytmgc[claude]\""
            ) from exc

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=config.api_key)
        self._config = config
        self._taxonomy = taxonomy
        self._reference = build_reference(taxonomy)
        self._schema = _schema(taxonomy.genres(), taxonomy.moods())

    # ------------------------------------------------------------- requêtes

    def _system(self) -> list[dict[str, Any]]:
        """Préambule commun, mis en cache : il est identique à chaque requête."""
        return [
            {"type": "text", "text": SYSTEM},
            {
                "type": "text",
                "text": self._reference,
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def _params(self, subjects: Sequence[Subject]) -> dict[str, Any]:
        return {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "system": self._system(),
            # La réflexion adaptative est ce qui distingue un jugement d'un
            # réflexe : c'est précisément ce qu'on paie ici.
            "thinking": {"type": "adaptive"},
            "output_config": {"format": {"type": "json_schema", "schema": self._schema}},
            "messages": [{"role": "user", "content": build_prompt(subjects)}],
        }

    @staticmethod
    def _text(message: Any) -> str:
        for block in message.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return ""

    # ------------------------------------------------------------- à l'unité

    def lookup(self, subjects: Sequence[Subject]) -> list[Verdict]:
        """Juge quelques titres tout de suite, hors lot.

        Sert la recherche manuelle : un titre ajouté après la passe complète ne
        justifie pas d'attendre un traitement par lots.
        """
        if not subjects:
            return []
        try:
            message = self._client.messages.create(**self._params(subjects))
        except self._anthropic.APIStatusError as exc:
            raise ClaudeError(f"L'API Claude a refusé la requête : {exc}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise ClaudeError("API Claude injoignable : vérifie la connexion.") from exc

        if getattr(message, "stop_reason", None) == "refusal":
            raise ClaudeError("Le modèle a refusé de répondre à cette demande.")
        return parse_verdicts(self._text(message), subjects)

    # ---------------------------------------------------------------- lots

    def submit(self, chunks: Sequence[Sequence[Subject]]) -> str:
        """Dépose un lot et rend son identifiant.

        L'identifiant est tout ce qu'il faut conserver : le lot vit côté
        Anthropic, l'ordinateur peut s'éteindre, et ses résultats restent
        récupérables pendant vingt-neuf jours.
        """
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        requests = [
            Request(
                custom_id=f"lot-{index}",
                params=MessageCreateParamsNonStreaming(**self._params(subjects)),
            )
            for index, subjects in enumerate(chunks)
        ]
        try:
            batch = self._client.messages.batches.create(requests=requests)
        except self._anthropic.APIStatusError as exc:
            raise ClaudeError(f"Dépôt du lot refusé : {exc}") from exc
        return batch.id

    def status(self, batch_id: str) -> tuple[str, dict[str, int]]:
        """État du lot : ("ended" | "in_progress" | ..., compteurs par issue)."""
        batch = self._client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        return batch.processing_status, {
            "processing": getattr(counts, "processing", 0),
            "succeeded": getattr(counts, "succeeded", 0),
            "errored": getattr(counts, "errored", 0),
            "canceled": getattr(counts, "canceled", 0),
            "expired": getattr(counts, "expired", 0),
        }

    def collect(
        self, batch_id: str, chunks: Sequence[Sequence[Subject]]
    ) -> tuple[list[Verdict], list[str]]:
        """Récupère les verdicts d'un lot terminé, et ce qui a échoué.

        Un lot partiellement réussi n'est pas une perte : les verdicts obtenus
        sont conservés, et seuls les titres manquants repartiront à la passe
        suivante — le fichier de verdicts s'en charge tout seul.
        """
        verdicts: list[Verdict] = []
        problems: list[str] = []
        for result in self._client.messages.batches.results(batch_id):
            index = _chunk_index(result.custom_id)
            if index is None or index >= len(chunks):
                problems.append(f"{result.custom_id} : lot inconnu, ignoré")
                continue
            kind = result.result.type
            if kind == "succeeded":
                verdicts.extend(parse_verdicts(self._text(result.result.message), chunks[index]))
            else:
                problems.append(f"{result.custom_id} : {kind}")
        return verdicts, problems

    def cancel(self, batch_id: str) -> None:
        self._client.messages.batches.cancel(batch_id)


def _chunk_index(custom_id: str) -> int | None:
    prefix, _, number = custom_id.partition("-")
    if prefix != "lot" or not number.isdigit():
        return None
    return int(number)


def subjects_from(
    tracks: Iterable[Track], classifications: dict[str, Classification]
) -> list[Subject]:
    return [Subject.of(track, classifications.get(track.video_id)) for track in tracks]
