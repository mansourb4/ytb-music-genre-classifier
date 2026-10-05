"""Pays d'origine des artistes, tenus dans un fichier texte versionné.

Le verdict décrit une musique, pas d'où elle vient. Or certaines familles se
rangent d'abord par pays : on n'écoute pas le rap français et le rap
américain dans la même playlist. Le pays est une propriété de l'artiste, pas
du morceau : il vit donc dans `data/pays.txt`, une ligne par artiste, et une
règle du plan le désigne par `pays = ["FR", "BE"]`.

Format : `artiste <tab> code pays` (FR, BE, US, UK…). Un artiste absent du
fichier n'a pas de pays connu : aucune règle de pays ne le retient.
"""

from __future__ import annotations

import re
from pathlib import Path

from ytmgc.matching.normalize import normalize_artist

HEADER = """\
# Pays d'origine des artistes ytmgc — pour les règles « pays » du plan.
#
# Une ligne par artiste, colonnes séparées par une tabulation :
#   artiste  |  code pays (FR, BE, CH, US, CA, UK, IE, MA, DZ, IT, DE, JP…)
#
# La casse, les accents et les chaînes « - Topic » ou « VEVO » sont
# indifférents. Un artiste absent n'a pas de pays connu.
"""

CODE_RE = re.compile(r"^[A-Z]{2,3}$")

#: Dans une règle, `pays = ["?"]` retient les titres dont aucun artiste n'a de
#: pays connu : c'est la playlist d'attente, qu'on vide en complétant le fichier.
UNKNOWN_COUNTRY = "?"


def parse_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip("\n")
    if not stripped.strip() or stripped.lstrip().startswith("#"):
        return None
    parts = [part.strip() for part in stripped.split("\t")]
    if len(parts) < 2 or not parts[0] or not CODE_RE.match(parts[1].upper()):
        return None
    return parts[0], parts[1].upper()


def load(path: str | Path) -> dict[str, str]:
    """Pays de chaque artiste, indexé par nom d'artiste normalisé."""
    file = Path(path)
    if not file.exists():
        return {}
    origins: dict[str, str] = {}
    for line in file.read_text(encoding="utf-8").splitlines():
        entry = parse_line(line)
        if entry is not None:
            origins[normalize_artist(entry[0])] = entry[1]
    return origins


def save(entries: dict[str, str], path: str | Path) -> None:
    """Écrit le fichier trié par artiste, atomiquement. `entries` : nom tel
    qu'affiché -> code pays."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{artist}\t{code}" for artist, code in
             sorted(entries.items(), key=lambda item: item[0].casefold())]
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(file)


def countries_of(artists: tuple[str, ...], origins) -> set[str]:
    """Pays d'un titre : ceux de ses artistes, invités compris."""
    return {origins[key] for key in (normalize_artist(a) for a in artists) if key in origins}
