"""Révision complète par un autre modèle : un prompt, une réponse, tout appliqué.

Trier titre par titre reste trop long, même en ne tranchant que des
propositions. Ici, un modèle à très grande fenêtre (Gemini) reçoit d'un coup
toute la bibliothèque — chaque playlist, sa description, sa règle, et tous
ses titres avec ce que le verdict en dit — et a les mains libres : déplacer,
renommer, supprimer, créer des playlists.

Sa réponse suit un format strict, que ce module lit puis applique sans
intervention :

    PLAYLISTS          la liste finale, complète : « nom | description »
    RENOMMAGES         « ancien nom -> nouveau nom »
    DEPLACEMENTS       « T0042 | nom de playlist » pour chaque titre qui change
    FIN

Une playlist actuelle absente de la liste finale, et non renommée, est
supprimée. Les titres sont désignés par un numéro court, tenu dans
`pistes.tsv` à côté du prompt : la réponse est plus courte et ne dépend pas
de l'orthographe d'un titre.

Ce que l'utilisateur a décidé lui-même — déplacement, verdict validé — est
marqué « [fixé] » et n'est jamais déplacé, sauf si sa playlist disparaît.

Le tri complet (`build_complete`) va plus loin : Gemini reçoit la liste des
titres et, en simple inspiration, les playlists actuelles — sans savoir quel
titre est où ni par quelle règle. Il fait ses propres playlists et
range chaque titre — y compris ceux fixés à la main. Sa réponse remplace
DEPLACEMENTS par CLASSEMENT, une ligne par titre ; chaque titre classé reçoit
un déplacement, même s'il reste où il était : c'est Gemini qui l'a rangé.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ytmgc.matching.normalize import fold, slugify
from ytmgc.placements import NOWHERE
from ytmgc.verdicts import entry_key

PROMPT_FILE = "prompt.txt"
TRACKS_FILE = "pistes.tsv"
FIXED = "[fixé]"

SECTIONS = ("PLAYLISTS", "RENOMMAGES", "DEPLACEMENTS", "CLASSEMENT", "FIN")
_ID_RE = re.compile(r"^T\d{4,5}$")


class GeminiError(ValueError):
    """Réponse inutilisable, avec ce qu'il faut faire."""


# ------------------------------------------------------------------ export


@dataclass(frozen=True, slots=True)
class Row:
    """Un titre du prompt : son numéro, son identité, sa place actuelle."""

    id: str
    artist: str
    title: str
    playlist: str

    @property
    def key(self) -> str:
        return entry_key(self.artist, self.title)


def _clean(text: str) -> str:
    return " ".join(str(text).replace("|", "/").split())


INTRO = """\
Tu es musicologue. Tu vas réorganiser la discothèque de quelqu'un en
playlists, et tu as les mains libres.

CE QUE LA PERSONNE VEUT

1. Comprendre ce qu'elle écoute : chaque playlist porte un nom qui dit ce
   qu'on y entend, rangé par famille musicale (« Jazz · Fusion planante »,
   « Rap · FR sombre »). Pas de fourre-tout du type « Monde » ou « Divers ».
2. Ne jamais être surprise quand elle lance une playlist : du premier au
   dernier titre, la même vibe — même énergie, même tempo, même univers
   sonore. Un titre qui détonne est mal rangé, même si son genre est le bon.

Choix déjà faits par la personne, à respecter :
- Le rap se range d'abord par pays (FR, US, UK, Maghreb, Ailleurs), puis le
  rap français par ambiance.
- La variété et la chanson françaises ont leur playlist.
- Les musiques de film vont dans le genre de leur musique, pas dans une
  playlist « BO ».
- Une playlist tient entre 5 et 150 titres environ ; plus petite, elle doit
  vraiment se justifier, plus grande, elle mérite d'être coupée en deux
  ambiances.

CE QUI A ÉTÉ FAIT JUSQU'ICI

Chaque titre a reçu un genre, un style, une ambiance et une courte note, puis
des règles l'ont versé dans une playlist. Les règles se trompent souvent sur
la vibe. Ci-dessous, chaque playlist actuelle avec sa description, sa règle
et tous ses titres, au format :

    numéro | artiste | titre | genre / style · ambiance | note

Les titres marqués [fixé] ont été placés à la main par la personne : ne les
déplace jamais, sauf si tu supprimes leur playlist (donne-leur alors une
nouvelle place).

TON TRAVAIL

Révise tout. N'hésite pas à :
- déplacer les titres qui cassent la vibe de leur playlist ;
- renommer une playlist dont le nom ne dit pas ce qu'on y entend ;
- supprimer une playlist (ses titres doivent alors tous être déplacés) ;
- en fusionner deux trop proches, ou en couper une trop large ;
- en créer de nouvelles quand un ensemble de titres le mérite.
Mieux vaut peu de changements justes que beaucoup d'hésitants : chaque
playlist doit sonner juste de bout en bout.
"""

FORMAT = """\
FORMAT DE LA RÉPONSE — À RESPECTER À LA LETTRE

D'abord, en quelques lignes de texte libre, ce que tu as changé et pourquoi.

Puis un seul bloc de code contenant exactement ces sections, dans cet ordre :

```
PLAYLISTS
Famille · Nom | description en une phrase de ce qu'on y entend
(la liste FINALE et COMPLÈTE des playlists, y compris celles que tu ne
changes pas ; toute playlist actuelle absente de cette liste et non renommée
sera supprimée)

RENOMMAGES
Ancien nom exact -> Nouveau nom exact
(une ligne par playlist renommée ; ses titres la suivent sans être listés)

DEPLACEMENTS
T0042 | Nom exact d'une playlist de la liste finale
(une ligne par titre qui change de playlist, et seulement ceux-là ; pour ne
ranger un titre nulle part, écris « (aucune) » comme playlist)

FIN
```

Règles du format :
- Recopie les noms à l'identique, accents et « · » compris.
- Un titre d'une playlist supprimée doit figurer dans DEPLACEMENTS.
- N'écris jamais « | » dans un nom ou une description.
- Si ta réponse ne tient pas en un message, arrête-toi à la fin d'une ligne :
  on t'écrira « continue » et tu reprendras à la ligne suivante, dans un
  nouveau bloc, sans répéter ce qui est déjà écrit.
"""


PARTIAL = """\
CETTE FOIS, UNE PARTIE SEULEMENT

La bibliothèque est revue en plusieurs fois. Ne révise que les playlists dont
les titres sont listés ci-dessous. Les autres sont seulement nommées, avec
leur description : tu peux y déplacer des titres, les renommer, mais pas les
supprimer. Recopie quand même toutes les playlists dans la section PLAYLISTS.
"""


def build(summary, plan, tracks: dict, fixed_keys: set[str], *,
          only: set[str] | None = None, start: int = 1) -> tuple[str, list[Row]]:
    """Le prompt, et la table des numéros de titres.

    `summary` est l'aperçu « Par famille » ; `tracks` : identifiant vidéo ->
    Track, pour retrouver artiste et titre des non rangés. Avec `only`, seules
    ces playlists listent leurs titres : les autres ne sont que nommées, et
    la numérotation commence à `start`.
    """
    written = plan.by_key()
    rows: list[Row] = []
    seen: set[str] = set()
    blocks: list[str] = []

    def number(artist: str, title: str, playlist: str) -> Row | None:
        key = entry_key(artist, title)
        if key in seen:
            return None
        seen.add(key)
        row = Row(f"T{len(rows) + start:04d}", artist, title, playlist)
        rows.append(row)
        return row

    for playlist in summary.playlists:
        rules = written.get(playlist.key)
        if only is not None and playlist.name not in only:
            head = [f"## {playlist.name} — {len(playlist.tracks)} titres (déjà revue, titres non listés)"]
            if rules and rules.description:
                head.append(f"Description : {rules.description}")
            blocks.append("\n".join(head))
            continue
        lines = []
        for track in playlist.tracks:
            artist = track.main_artist or track.artist
            row = number(artist, track.title, playlist.name)
            if row is None:
                continue
            what = " / ".join([*track.genres[:1], *track.styles[:2]])
            if track.mood:
                what += f" · {track.mood}"
            fixed = " " + FIXED if (track.moved or row.key in fixed_keys) else ""
            lines.append(" | ".join([row.id, _clean(artist), _clean(track.title), what,
                                     _clean(track.note)]) + fixed)
        head = [f"## {playlist.name} — {len(lines)} titres"]
        if rules and rules.description:
            head.append(f"Description : {rules.description}")
        criteria = [rule.describe() for rule in rules.rules] if rules else []
        head.append("Règle : " + (" ; ou ".join(c for c in criteria if c)
                                   or "aucune, remplie à la main"))
        blocks.append("\n".join([*head, "", *lines]))

    loose = []
    for entry in (summary.unsorted if only is None else []):
        if entry.get("reason") == "kept_out":
            continue
        track = tracks.get(entry["video_id"])
        if track is None:
            continue
        from ytmgc.verdicts import main_artist

        row = number(main_artist(track), track.title, NOWHERE)
        if row is not None:
            loose.append(" | ".join([row.id, _clean(row.artist), _clean(row.title),
                                     _clean(entry.get("detail", "")), "rangé nulle part"]))
    if loose:
        blocks.append("\n".join(["## Non rangés — aucune playlist ne les accepte", "", *loose]))

    empty = [p for p in plan.playlists if p.key not in {pl.key for pl in summary.playlists}]
    names = sorted({p.name for p in empty})
    header = (
        f"BIBLIOTHÈQUE : {len(rows)} titres, {len(summary.playlists)} playlists.\n"
        + (f"Playlists du plan encore vides : {', '.join(names)}.\n" if names else "")
    )
    intro = INTRO + ("\n" + PARTIAL if only is not None else "")
    prompt = "\n".join([intro, FORMAT, header, "PLAYLISTS ACTUELLES ET LEURS TITRES", "",
                        "\n\n".join(blocks), "", FORMAT])
    return prompt, rows


COMPLETE_INTRO = """\
Tu es musicologue. Voici toute la discothèque d'une personne : {count}
titres. Range-la entièrement en playlists, comme tu l'entends.

Ce qu'elle attend de ses playlists :
- comprendre ce qu'elle écoute : le nom d'une playlist dit ce qu'on y entend ;
- garder la même vibe du premier au dernier titre d'une playlist.

Tu as carte blanche : crée autant de playlists que nécessaire, de la taille
que tu veux, nommées comme tu veux. Inspire-toi des playlists actuelles de la
personne, listées plus bas avec quelques-uns de leurs artistes : elles disent
comment elle aime découper sa musique. Reprends celles qui sonnent juste
(sous leur nom exact), améliore, fusionne ou coupe les autres, oublie celles
qui ne tiennent pas, invente celles qui manquent. Pour trouver le
genre de chaque titre, utilise internet et toutes les sources utiles
(Discogs, MusicBrainz, AllMusic, Wikipédia, Rate Your Music, Last.fm,
Bandcamp, YouTube…) plutôt que de deviner, surtout pour les artistes peu
connus. Tous les titres doivent être rangés ; un titre qui n'est pas de la
musique (podcast, extrait de film, bruit…) peut aller dans « (aucune) ».

Les titres sont triés par artiste, au format :

    numéro | artiste | titre
"""

COMPLETE_FORMAT = """\
FORMAT DE LA RÉPONSE — À RESPECTER À LA LETTRE

Ta réponse sera lue par un programme. Elle se fait en plusieurs messages :
la liste est trop longue pour un seul.

Premier message : en quelques lignes, ta logique de rangement. Puis un bloc
de code avec la liste COMPLÈTE de tes playlists, et le début du classement :

```
PLAYLISTS
Nom de la playlist | description en une phrase de ce qu'on y entend

CLASSEMENT
T0001 | artiste | titre | Nom exact de la playlist | genre précis trouvé
T0002 | artiste | titre | Nom exact de la playlist | genre précis trouvé
```

Arrête-toi après environ 400 titres, à la fin d'une ligne. On t'écrira
« continue » : reprends au titre suivant, dans un nouveau bloc de code qui
commence par CLASSEMENT, sans rien répéter. Quand le dernier titre est
classé, termine le bloc par une ligne FIN.

Règles du format :
- Une ligne par titre, dans l'ordre des numéros, sans en sauter un seul.
- Recopie l'artiste et le titre tels qu'ils sont dans la liste : c'est ce
  qui permet de vérifier que chaque numéro désigne bien le bon titre.
- Le nom de playlist est recopié à l'identique de ta section PLAYLISTS, avec
  son « · » : « Famille · Nom ».
- Le genre précis tient en quelques mots (« deep house », « bossa nova »,
  « rap français cloud »).
- N'écris jamais « | » dans un nom, une description ou un genre.
- Ne change pas la liste des playlists en cours de route.
"""


def _inspiration(summary, plan) -> list[str]:
    """Les playlists actuelles, en exemples : nom, taille, description, artistes."""
    written = plan.by_key() if plan is not None else {}
    lines = []
    for playlist in summary.playlists:
        rules = written.get(playlist.key)
        artists = Counter(t.main_artist or t.artist for t in playlist.tracks)
        line = f"- {playlist.name} ({len(playlist.tracks)} titres)"
        if rules and rules.description:
            line += f" : {_clean(rules.description)}"
        top = ", ".join(_clean(a) for a, _ in artists.most_common(INSPIRATION_ARTISTS))
        lines.append(line + (f" — ex. {top}" if top else ""))
    return lines


#: Artistes cités en exemple pour chaque playlist actuelle.
INSPIRATION_ARTISTS = 6


UNSURE_INTRO = """\
Tu es musicologue. Une personne a rangé sa discothèque en {playlists}
playlists. Pour {count} de ses titres, le genre est incertain : souvent des
artistes peu connus. Vérifie chacun d'eux et range-le dans la bonne
playlist.

Ce qu'elle attend de ses playlists :
- comprendre ce qu'elle écoute : le nom d'une playlist dit ce qu'on y entend ;
- garder la même vibe du premier au dernier titre d'une playlist.

Pour chaque titre, cherche sur internet ce qu'il est vraiment (Discogs,
MusicBrainz, AllMusic, Bandcamp, Rate Your Music, Last.fm, YouTube…) plutôt
que de deviner, puis choisis la playlist où il sonne juste. S'il y est déjà,
garde-la. Range-le uniquement dans une playlist de la liste ci-dessous : n'en
crée pas, n'en renomme pas. « (aucune) » est réservé à ce qui n'est pas de la
musique (podcast, annonce, tutoriel, bruit) : un DJ set, un mix, une reprise
ou une musique de jeu vidéo sont de la musique, range-les.

LES PLAYLISTS (nom, taille, description, quelques artistes)

{inspiration}
"""

UNSURE_FORMAT = """\
FORMAT DE LA RÉPONSE — À RESPECTER À LA LETTRE

Ta réponse sera lue par un programme. Un bloc de code, une ligne par titre,
CINQ colonnes : le numéro, l'artiste et le titre recopiés tels qu'ils sont
écrits dans la liste, PUIS LA PLAYLIST CHOISIE, puis le genre trouvé.

```
CLASSEMENT
T0001 | artiste | titre | Nom exact de la playlist | genre précis trouvé
FIN
```

- Tous les titres, dans l'ordre, sans en sauter ni en répéter.
- La playlist est recopiée à l'identique de la liste, « · » compris.
- Si tout ne tient pas en un message, arrête-toi après environ 200 titres :
  on t'écrira « continue », reprends au titre suivant dans un nouveau bloc
  qui commence par CLASSEMENT. Après le dernier titre, une ligne FIN.
- N'écris jamais « | » dans un nom ou un genre.
"""


def build_unsure(summary, plan=None) -> tuple[str, list[Row]]:
    """Le prompt de relecture des titres « à vérifier » : ces titres seulement,
    à ranger dans les playlists actuelles, sans en créer ni en supprimer.

    La réponse se lit comme un tri complet sans section PLAYLISTS : chaque
    titre classé reçoit un déplacement et n'est plus à vérifier.
    """
    found: dict[str, Row] = {}
    for playlist in summary.playlists:
        for track in playlist.tracks:
            if track.unsure:
                artist = track.main_artist or track.artist
                found.setdefault(entry_key(artist, track.title),
                                 Row("", artist, track.title, playlist.name))
    ordered = sorted(found.values(), key=lambda r: (fold(r.artist), fold(r.title)))
    rows = [Row(f"T{i:04d}", r.artist, r.title, r.playlist)
            for i, r in enumerate(ordered, start=1)]
    lines = [" | ".join([r.id, _clean(r.artist), _clean(r.title), f"actuellement : {r.playlist}"])
             for r in rows]
    intro = UNSURE_INTRO.format(playlists=len(summary.playlists), count=len(rows),
                                inspiration="\n".join(_inspiration(summary, plan)))
    prompt = "\n".join([intro, UNSURE_FORMAT, "LES TITRES À VÉRIFIER", "", *lines, "",
                        UNSURE_FORMAT])
    return prompt, rows


def build_complete(summary, tracks: dict, plan=None) -> tuple[str, list[Row]]:
    """Le prompt du tri complet : tous les titres, et les playlists actuelles en
    simple inspiration — sans dire quel titre est où, ni par quelle règle.

    Les titres écartés par l'utilisateur (« (aucune) ») restent hors du prompt :
    il ne les veut dans aucune playlist.
    """
    from ytmgc.verdicts import main_artist

    found: dict[str, tuple[str, str, str]] = {}
    for playlist in summary.playlists:
        for track in playlist.tracks:
            artist = track.main_artist or track.artist
            found.setdefault(entry_key(artist, track.title), (artist, track.title, playlist.name))
    for entry in summary.unsorted:
        track = tracks.get(entry["video_id"])
        if entry.get("reason") == "kept_out" or track is None:
            continue
        found.setdefault(entry_key(main_artist(track), track.title),
                         (main_artist(track), track.title, NOWHERE))
    ordered = sorted(found.values(), key=lambda t: (fold(t[0]), fold(t[1])))
    rows = [Row(f"T{i:04d}", a, t, p) for i, (a, t, p) in enumerate(ordered, start=1)]
    lines = [" | ".join([r.id, _clean(r.artist), _clean(r.title)]) for r in rows]
    inspiration = _inspiration(summary, plan)
    prompt = "\n".join([COMPLETE_INTRO.format(count=len(rows)), COMPLETE_FORMAT,
                        "PLAYLISTS ACTUELLES, POUR T'EN INSPIRER", "", *inspiration, "",
                        "LES TITRES", "", *lines, "", COMPLETE_FORMAT])
    return prompt, rows


RETRY = """\
Dans certains de tes messages, la numérotation a glissé : tu as sauté ou
répété un titre, puis continué à numéroter, si bien que chaque numéro a reçu
la playlist de son voisin (par exemple {example}).

Reclasse les {count} titres ci-dessous, et seulement eux ({first} à {last}),
dans tes playlists — les voici, recopie leur nom à l'identique :

{playlists}
- (aucune) — seulement pour ce qui n'est pas de la musique

Chaque ligne a CINQ colonnes : numéro, artiste et titre recopiés tels
qu'ils sont écrits ici, PUIS LE NOM DE LA PLAYLIST, puis le genre précis.
La playlist est obligatoire : une ligne sans playlist est perdue.

```
CLASSEMENT
{first} | artiste | titre | Nom exact de la playlist | genre précis trouvé
```

Environ 300 titres par message, dans un bloc de code qui commence par
CLASSEMENT ; on t'écrira « continue ». Une ligne par titre, sans en sauter
ni en répéter. Après le dernier ({last}), une ligne FIN.

LES TITRES À RECLASSER

{lines}
"""


def build_retry(rows: dict[str, Row], ids: list[str], example: str,
                playlists: list[str] = ()) -> str:
    """Le message qui redemande le classement de certains titres, avec
    artiste et titre recopiés sur chaque ligne pour qu'aucun numéro ne glisse,
    et la liste des playlists à utiliser."""
    lines = [" | ".join([i, _clean(rows[i].artist), _clean(rows[i].title)]) for i in ids]
    return RETRY.format(example=example, count=len(ids), first=ids[0], last=ids[-1],
                        playlists="\n".join(f"- {name}" for name in playlists),
                        lines="\n".join(lines))


def save_rows(rows: list[Row], path: str | Path) -> None:
    lines = ["# Numéros des titres du prompt Gemini : numéro | artiste | titre | playlist d'alors"]
    lines += ["\t".join([r.id, r.artist.replace("\t", " "), r.title.replace("\t", " "),
                         r.playlist]) for r in rows]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_rows(path: str | Path) -> dict[str, Row]:
    rows = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 4:
            rows[parts[0]] = Row(*parts[:4])
    return rows


# ---------------------------------------------------------------- lecture


@dataclass(slots=True)
class Answer:
    playlists: list[tuple[str, str]] = field(default_factory=list)
    renames: dict[str, str] = field(default_factory=dict)
    moves: dict[str, str] = field(default_factory=dict)
    finished: bool = False
    #: Réponse au tri complet : chaque titre est classé, pas seulement déplacé.
    complete: bool = False
    #: Les lignes qui recopient artiste et titre, dans l'ordre : (numéro,
    #: (artiste, titre), playlist). De quoi vérifier chaque numéro.
    labelled: list[tuple[str, tuple[str, str], str]] = field(default_factory=list)
    #: Les champs bruts de ces lignes : un titre peut contenir « | », la
    #: colonne de la playlist ne se trouve qu'une fois la liste connue.
    raw: dict[int, list[str]] = field(default_factory=dict)


def _section(line: str) -> str | None:
    word = fold(line.strip("#*:` ").strip())
    for name in SECTIONS:
        if word == fold(name) or word == fold(name).replace("deplacements", "déplacements"):
            return name
    return None


def parse(text: str) -> Answer:
    """Lit une réponse, éventuellement en plusieurs morceaux collés bout à bout."""
    answer = Answer()
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```") or line.startswith("("):
            continue
        section = _section(line)
        if section:
            current = section
            answer.finished |= section == "FIN"
            answer.complete |= section == "CLASSEMENT"
            continue
        if current == "PLAYLISTS" and "|" in line:
            name, _, description = line.partition("|")
            if name.strip():
                answer.playlists.append((name.strip(), description.strip()))
        elif current == "PLAYLISTS" and line:
            answer.playlists.append((line, ""))
        elif current == "RENOMMAGES":
            for arrow in ("->", "→", "=>"):
                if arrow in line:
                    old, _, new = line.partition(arrow)
                    if old.strip() and new.strip():
                        answer.renames[old.strip()] = new.strip()
                    break
        elif current in ("DEPLACEMENTS", "CLASSEMENT") and "|" in line:
            track, *fields = [f.strip() for f in line.split("|")]
            if not _ID_RE.match(track):
                continue
            # « T | playlist [| genre] », ou « T | artiste | titre | playlist [| genre] » ;
            # le genre trouvé n'est pas lu.
            playlist = (fields[2] if len(fields) >= 3 else fields[0]).strip("«» ")
            if not playlist:
                continue
            if len(fields) >= 3:
                answer.raw[len(answer.labelled)] = fields
                answer.labelled.append((track, (fields[0], fields[1]), playlist))
            answer.moves.pop(track, None)  # une ligne reprise plus loin remplace l'ancienne
            answer.moves[track] = playlist
    if not answer.playlists and not (answer.complete and answer.moves):
        raise GeminiError(
            "Aucune section PLAYLISTS dans cette réponse. Colle la réponse entière de "
            "Gemini, bloc de code compris."
        )
    return answer


# -------------------------------------------------------------- application


@dataclass(slots=True)
class Change:
    """Ce que la réponse fait au plan et aux déplacements."""

    renamed: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    created: list[tuple[str, str]] = field(default_factory=list)
    described: dict[str, str] = field(default_factory=dict)
    moves: dict[str, str] = field(default_factory=dict)
    kept_fixed: list[str] = field(default_factory=list)
    #: Tri complet : titres classés là où ils étaient déjà.
    confirmed: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def lines(self, rows: dict[str, Row]) -> list[str]:
        out = [
            f"{len(self.renamed)} playlist(s) renommée(s)",
            f"{len(self.deleted)} supprimée(s)",
            f"{len(self.created)} créée(s)",
            f"{len(self.described)} description(s) mise(s) à jour",
            f"{len(self.moves)} titre(s) déplacé(s)",
        ]
        if self.confirmed:
            out.append(f"{len(self.confirmed)} laissé(s) à leur place")
        details = [f"  renommée : {a} -> {b}" for a, b in self.renamed.items()]
        details += [f"  supprimée : {name}" for name in self.deleted]
        details += [f"  créée : {name}" for name, _ in self.created]
        if self.kept_fixed:
            details.append(f"  {len(self.kept_fixed)} titre(s) [fixé] laissé(s) en place")
        details += [f"  ⚠ {p}" for p in self.problems]
        return [", ".join(out) + "."] + details


def _resolver(names: list[str]):
    exact = {fold(n): n for n in names}

    def resolve(name: str) -> str | None:
        if fold(name) in (fold(NOWHERE), "aucune"):
            return NOWHERE
        return exact.get(fold(name))

    return resolve


def _loose(name: str) -> str:
    """Un nom de playlist, sans ce que les modèles réécrivent à leur guise :
    le « · », « & » devenu « et », « R&B » devenu « RnB »."""
    words = {"and": "et", "n": "et", "rnb": "r et b"}  # fold() écrit « & » « and »
    return " ".join(words.get(w, w) for w in fold(name).replace("·", " ").split())


def canonical_names(answer: Answer, current: list[str]) -> dict[str, str]:
    """Remet les noms de la réponse à l'orthographe du plan.

    Un nom qui ne diffère d'une playlist actuelle que par la ponctuation est
    cette playlist ; une playlist dont seule la famille change (« Funk · X »
    devenue « Orient X ») est renommée ; un nom nouveau sans « · » le reçoit
    après sa famille. Renvoie les renommages ainsi trouvés.
    """
    by_loose = {_loose(n): n for n in current}
    families = {_loose(n.split("·")[0]) for n in current if "·" in n}
    by_rest: dict[str, list[str]] = {}
    for n in current:
        if "·" in n:
            by_rest.setdefault(_loose(n.split("·", 1)[1]), []).append(n)
    taken = {by_loose[_loose(n)] for n, _ in answer.playlists if _loose(n) in by_loose}
    mapping: dict[str, str] = {}
    renames: dict[str, str] = {}
    for name, _ in answer.playlists:
        if fold(name) in (fold(NOWHERE), "aucune"):
            continue
        if _loose(name) in by_loose:
            mapping[name] = by_loose[_loose(name)]
            continue
        fixed = name
        if "·" not in name:
            head, _, rest = name.partition(" ")
            if rest and _loose(head) in families:
                fixed = f"{head} · {rest}"
        mapping[name] = fixed
        if "·" in fixed:
            same = by_rest.get(_loose(fixed.split("·", 1)[1]), [])
            if len(same) == 1 and same[0] not in taken and same[0] not in renames:
                renames[same[0]] = fixed
    answer.playlists = [(mapping.get(n, n), d) for n, d in answer.playlists]
    final = {_loose(n): n for n, _ in answer.playlists}

    def target(name: str) -> str:
        """Un nom de playlist d'une ligne de classement : celui de la liste,
        même mal recopié (« Classique Classique et Baroque vif »)."""
        if name in mapping:
            return mapping[name]
        close = difflib.get_close_matches(_loose(name), list(final), n=1, cutoff=0.85)
        if close:
            return final[close[0]]
        same = [n for k, n in final.items() if set(k.split()) == set(_loose(name).split())]
        return same[0] if len(same) == 1 else name

    names = set(final.values()) | {NOWHERE}

    def split(fields: list[str]) -> tuple[tuple[str, str], str]:
        """(artiste, titre), playlist : la playlist est le premier champ, après
        l'artiste et le titre, qui en nomme une de la liste."""
        for i in range(2, len(fields)):
            name = NOWHERE if fold(fields[i]) in (fold(NOWHERE), "aucune") else target(fields[i])
            if name in names:
                return (fields[0], " | ".join(fields[1:i])), name
        return (fields[0], fields[1]), target(fields[2])

    labelled = []
    for n, (track, label, playlist) in enumerate(answer.labelled):
        if n in answer.raw:
            label, playlist = split(answer.raw[n])
        labelled.append((track, label, playlist))
        answer.moves[track] = playlist
    answer.labelled = labelled
    answer.moves = {t: target(p) for t, p in answer.moves.items()}
    answer.renames = {**renames, **{o: mapping.get(n, n) for o, n in answer.renames.items()}}
    return renames


def _likeness(label: tuple[str, str], row: Row) -> float:
    """À quel point l'artiste et le titre recopiés ressemblent à ceux du titre
    (1 : la même chanson, aux fautes de recopie près)."""
    artist, title = label
    if entry_key(artist, title) == row.key:
        return 1.0
    return difflib.SequenceMatcher(None, fold(f"{artist} {title}"),
                                   fold(f"{row.artist} {row.title}")).ratio()


#: Ressemblance minimale pour rattacher une ligne mal recopiée à un titre.
SAME_TRACK = 0.9


def realign(answer: Answer, rows: dict[str, Row]) -> tuple[int, list[str]]:
    """Vérifie, pour chaque ligne qui recopie artiste et titre, que son numéro
    désigne bien ce titre ; sinon la rattache au bon numéro, ou l'écarte.

    Un modèle qui saute ou répète une ligne décale toute la suite de sa
    numérotation : sans cette vérification, chaque titre recevrait la playlist
    de son voisin. Renvoie le nombre de lignes recalées, et celles écartées.
    """
    if not answer.labelled:
        return 0, []
    by_key = {row.key: row.id for row in rows.values()}
    labelled = {track for track, _, _ in answer.labelled}
    # Les lignes sans artiste ni titre restent telles quelles.
    moves = {t: p for t, p in answer.moves.items() if t not in labelled}
    fixed, rejected = 0, []
    for track, label, playlist in answer.labelled:
        row = rows.get(track)
        found = by_key.get(entry_key(*label))
        if found is None:
            # Pas de correspondance exacte : le titre le plus ressemblant, autour.
            n = int(track[1:])
            near = [f"T{i:04d}" for i in range(max(1, n - 30), n + 31) if f"T{i:04d}" in rows]
            score, best = max(((_likeness(label, rows[i]), i) for i in near), default=(0, None))
            found = best if score >= SAME_TRACK else None
        if found == track and row is not None:
            moves[track] = playlist
            continue
        if found is None:
            rejected.append(track)
            continue
        moves[found] = playlist
        fixed += 1
    answer.moves = moves
    return fixed, rejected


def plan_changes(answer: Answer, rows: dict[str, Row], current: list[str],
                 fixed_keys: set[str]) -> Change:
    """Ce que la réponse demande, vérifié contre le plan actuel et les titres.

    Pour un tri complet, rien n'est fixé et un titre classé là où il était est
    gardé dans `confirmed` : il recevra lui aussi un déplacement.
    """
    if answer.complete:
        fixed_keys = set()
    if not answer.playlists:
        # Une relecture de quelques titres : les playlists restent celles du plan.
        answer.playlists = [(name, "") for name in current]
    canonical_names(answer, current)
    realigned, rejected = realign(answer, rows)
    change = Change()
    if realigned:
        change.problems.append(f"{realigned} ligne(s) au numéro décalé, rattachée(s) au bon titre")
    if rejected:
        change.problems.append(
            f"{len(rejected)} ligne(s) dont l'artiste et le titre ne correspondent à aucun titre, "
            f"ignorée(s) ({', '.join(rejected[:5])}…)")
    final = [name for name, _ in answer.playlists if fold(name) not in (fold(NOWHERE), "aucune")]
    resolve_final = _resolver(final)
    resolve_current = _resolver(current)

    for old, new in answer.renames.items():
        source, target = resolve_current(old), resolve_final(new)
        if source is None or source == NOWHERE:
            change.problems.append(f"renommage ignoré : « {old} » n'est pas une playlist actuelle")
        elif target is None:
            change.problems.append(f"renommage ignoré : « {new} » absent de la liste finale")
        elif source != target:
            change.renamed[source] = target
    after = {change.renamed.get(n, n) for n in current}
    change.deleted = [n for n in current if n not in change.renamed
                      and resolve_final(n) is None]
    change.created = [(n, d) for n, d in answer.playlists
                      if resolve_current(n) is None and n not in after]
    change.described = {n: d for n, d in answer.playlists if d and n in after}

    # Une playlist absente n'est supprimée que si tous ses titres ont une
    # nouvelle place : une réponse coupée ou distraite oublie des playlists
    # entières, et les supprimer jetterait leurs titres hors de tout rangement.
    members: dict[str, list[str]] = {}
    for row in rows.values():
        members.setdefault(row.playlist, []).append(row.id)
    forgotten = [n for n in change.deleted
                 if any(i not in answer.moves for i in members.get(n, []))]
    if forgotten:
        change.deleted = [n for n in change.deleted if n not in forgotten]
        change.problems.append(
            f"{len(forgotten)} playlist(s) absente(s) de la liste mais dont des titres n'ont pas "
            f"de nouvelle place, gardée(s) : {', '.join(forgotten)}"
        )
    deleted = set(change.deleted)
    for track, wanted in answer.moves.items():
        row = rows.get(track)
        if row is None:
            change.problems.append(f"{track} : numéro inconnu")
            continue
        target = resolve_final(wanted)
        if target is None and resolve_current(wanted) in forgotten:
            target = resolve_current(wanted)
        if target is None:
            change.problems.append(f"{track} : playlist « {wanted} » absente de la liste finale")
            continue
        now = change.renamed.get(row.playlist, row.playlist)
        if target == now:
            if answer.complete:
                change.confirmed[track] = target
            continue
        if row.key in fixed_keys and row.playlist not in deleted:
            change.kept_fixed.append(track)
            continue
        change.moves[track] = target
    if answer.complete:
        missing = [i for i in rows if i not in answer.moves]
        if missing:
            change.problems.append(
                f"{len(missing)} titre(s) non classé(s) par la réponse ({missing[0]}…) : "
                "ils restent où ils sont"
            )
    orphans = [r for r in rows.values() if r.playlist in deleted and r.id not in change.moves]
    if orphans:
        change.problems.append(
            f"{len(orphans)} titre(s) de playlists supprimées sans nouvelle place : "
            "ils suivront les règles restantes"
        )
    return change


def edit_plan_text(text: str, change: Change) -> str:
    """Le fichier du plan, modifié bloc par bloc : commentaires et ordre gardés."""
    from ytmgc.playlist_plan import MANUAL_SECTION, _toml_string

    lines = text.splitlines()
    out: list[str] = []
    i = 0
    described: set[str] = set()
    while i < len(lines):
        if lines[i].strip() != "[[playlist]]":
            out.append(lines[i])
            i += 1
            continue
        block = [lines[i]]
        i += 1
        while i < len(lines) and lines[i].strip() and lines[i].strip() != "[[playlist]]":
            block.append(lines[i])
            i += 1
        name = None
        for line in block:
            m = re.match(r'^nom\s*=\s*"(.*)"\s*$', line)
            if m:
                name = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
        if name in change.deleted:
            # Retire aussi la ligne vide qui suivait le bloc.
            if i < len(lines) and not lines[i].strip():
                i += 1
            continue
        new_name = change.renamed.get(name, name)
        rebuilt = []
        for line in block:
            if line.startswith("nom ") or line.startswith("nom="):
                rebuilt.append(f"nom = {_toml_string(new_name)}")
            elif line.startswith("description") and new_name in change.described:
                if new_name not in described:
                    rebuilt.append(f"description = {_toml_string(change.described[new_name])}")
                    described.add(new_name)
                else:
                    rebuilt.append(line)
            else:
                rebuilt.append(line)
        out.extend(rebuilt)
    text = "\n".join(out) + "\n"
    if change.created:
        if MANUAL_SECTION not in text:
            text += "\n\n" + MANUAL_SECTION + "\n"
        for name, description in change.created:
            text += f"\n[[playlist]]\nnom = {_toml_string(name)}\n"
            if description:
                text += f"description = {_toml_string(description)}\n"
            text += "manuelle = true\n"
    return text


def apply(answer: Answer, rows: dict[str, Row], config, taxonomy, *, write: bool) -> Change:
    """Applique la réponse au plan, aux déplacements et aux propositions.

    Sans `write`, ne fait que vérifier et décrire : rien n'est écrit.
    """
    from ytmgc import placements, playlist_review, verdicts
    from ytmgc.placements import Placement, PlacementBook
    from ytmgc.playlist_plan import load_plan, parse_plan

    plan_path = Path(config.taxonomy.playlists_file)
    plan = load_plan(plan_path, taxonomy)
    current = list(dict.fromkeys(p.name for p in plan.playlists))
    book = placements.load(config.taxonomy.placements_file)
    fixed = playlist_review.fixed_keys(verdicts.load(config.claude.verdicts_file), book)
    change = plan_changes(answer, rows, current, fixed)
    before = {}
    for playlist in plan.playlists:
        name = change.renamed.get(playlist.name, playlist.name)
        if playlist.description and name not in before:
            before[name] = playlist.description
    change.described = {n: d for n, d in change.described.items()
                         if before.get(n) != d and not (answer.complete and before.get(n))}

    text = edit_plan_text(plan_path.read_text(encoding="utf-8"), change)
    parse_plan(text, taxonomy)  # une faute ici arrête tout, avant toute écriture
    if not write:
        return change

    temporary = plan_path.with_suffix(plan_path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(plan_path)

    kept = {}
    for placement in book:
        name = change.renamed.get(placement.playlist, placement.playlist)
        kept[placement.key] = Placement(placement.artist, placement.title, name)
    for track, target in {**change.confirmed, **change.moves}.items():
        row = rows[track]
        kept[row.key] = Placement(row.artist, row.title, target)
    placements.save(PlacementBook(list(kept.values())), config.taxonomy.placements_file)
    # La révision tranche tout : les propositions d'avant n'ont plus d'objet.
    playlist_review.save(playlist_review.ProposalBook(), config.taxonomy.proposals_file)
    return change


def target_key(name: str) -> str:
    return "plan/" + slugify(name)
