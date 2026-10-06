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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ytmgc.matching.normalize import fold, slugify
from ytmgc.placements import NOWHERE
from ytmgc.verdicts import entry_key

PROMPT_FILE = "prompt.txt"
TRACKS_FILE = "pistes.tsv"
FIXED = "[fixé]"

SECTIONS = ("PLAYLISTS", "RENOMMAGES", "DEPLACEMENTS", "FIN")
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
        elif current == "DEPLACEMENTS" and "|" in line:
            track, _, playlist = line.partition("|")
            if _ID_RE.match(track.strip()) and playlist.strip():
                answer.moves[track.strip()] = playlist.strip().strip("«» ")
    if not answer.playlists:
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
    problems: list[str] = field(default_factory=list)

    def lines(self, rows: dict[str, Row]) -> list[str]:
        out = [
            f"{len(self.renamed)} playlist(s) renommée(s)",
            f"{len(self.deleted)} supprimée(s)",
            f"{len(self.created)} créée(s)",
            f"{len(self.described)} description(s) mise(s) à jour",
            f"{len(self.moves)} titre(s) déplacé(s)",
        ]
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


def plan_changes(answer: Answer, rows: dict[str, Row], current: list[str],
                 fixed_keys: set[str]) -> Change:
    """Ce que la réponse demande, vérifié contre le plan actuel et les titres."""
    change = Change()
    final = [name for name, _ in answer.playlists]
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
            continue
        if row.key in fixed_keys and row.playlist not in deleted:
            change.kept_fixed.append(track)
            continue
        change.moves[track] = target
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
    change.described = {n: d for n, d in change.described.items() if before.get(n) != d}

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
    for track, target in change.moves.items():
        row = rows[track]
        kept[row.key] = Placement(row.artist, row.title, target)
    placements.save(PlacementBook(list(kept.values())), config.taxonomy.placements_file)
    # La révision tranche tout : les propositions d'avant n'ont plus d'objet.
    playlist_review.save(playlist_review.ProposalBook(), config.taxonomy.proposals_file)
    return change


def target_key(name: str) -> str:
    return "plan/" + slugify(name)
