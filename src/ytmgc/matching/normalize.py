"""Normalisation des libellés avant comparaison.

Les titres YouTube Music portent beaucoup de bruit éditorial ("(Official Video)",
"feat. X", "- Remastered 2011") absent des métadonnées Discogs. On le retire
avant tout calcul de similarité.
"""

from __future__ import annotations

import re
import unicodedata

#: Segments parenthésés ou suffixés à supprimer d'un titre.
_NOISE_PATTERNS = (
    r"official\s+(music\s+)?(video|audio|lyric\s+video|visualizer)",
    r"lyrics?(\s+video)?",
    r"audio\s+only",
    r"hd|hq|4k",
    r"remaster(ed)?(\s+\d{4})?",
    r"\d{4}\s+remaster(ed)?",
    r"(deluxe|expanded|anniversary|special)\s+(edition|version)",
    r"radio\s+edit",
    r"explicit",
    r"free\s+download",
)
_NOISE_RE = re.compile(r"|".join(f"(?:{p})" for p in _NOISE_PATTERNS), re.IGNORECASE)

#: "feat. X", "ft X", "featuring X" jusqu'à la fin du segment. Les limites de
#: mots sont indispensables : sans elles, "ft" ampute "Daft Punk".
_FEAT_RE = re.compile(r"\s*\b(?:feat|ft|featuring)\b\.?\s+.*$", re.IGNORECASE)

#: Séparateurs d'artistes multiples.
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|\band\b|\bx\b|\bvs\.?\b|/|\+|;)\s*", re.IGNORECASE)

#: Suffixe numérique que Discogs ajoute aux homonymes : "Nirvana (2)".
_DISCOGS_HOMONYM_RE = re.compile(r"\s*\(\d+\)\s*$")

_BRACKETED_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def fold(text: str) -> str:
    """Minuscule, sans accents, sans ponctuation, espaces normalisés."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    stripped = stripped.replace("’", "'").replace("&", " and ")
    stripped = _PUNCT_RE.sub(" ", stripped.lower())
    return _WS_RE.sub(" ", stripped).strip()


def strip_noise(title: str) -> str:
    """Retire les mentions éditoriales d'un titre de piste.

    Un segment entre parenthèses n'est supprimé que s'il est *entièrement* du
    bruit : « (Deep House Mix) » ou « (Live at Wembley) » restent, car ils
    distinguent des versions différentes.
    """

    def _drop_if_noise(match: re.Match[str]) -> str:
        inner = match.group(1)
        residue = _NOISE_RE.sub("", inner)
        return "" if not fold(residue) else match.group(0)

    cleaned = _BRACKETED_RE.sub(_drop_if_noise, title)
    cleaned = re.sub(r"\s*-\s*" + _NOISE_RE.pattern + r"\s*$", "", cleaned, flags=re.IGNORECASE)
    return _WS_RE.sub(" ", cleaned).strip(" -–—")


def normalize_title(title: str) -> str:
    """Titre prêt à comparer : bruit retiré, featurings retirés, replié."""
    return fold(_FEAT_RE.sub("", strip_noise(title)))


def normalize_artist(artist: str) -> str:
    """Nom d'artiste replié, sans suffixe d'homonyme Discogs ni « The » initial."""
    cleaned = _DISCOGS_HOMONYM_RE.sub("", artist)
    folded = fold(_FEAT_RE.sub("", cleaned))
    return folded[4:] if folded.startswith("the ") else folded


def split_artists(raw: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Découpe une chaîne d'artistes multiples en noms normalisés distincts."""
    parts: list[str] = []
    values = [raw] if isinstance(raw, str) else list(raw)
    for value in values:
        for part in _ARTIST_SPLIT_RE.split(value):
            normalized = normalize_artist(part)
            if normalized and normalized not in parts:
                parts.append(normalized)
    return tuple(parts)


def tokens(text: str) -> frozenset[str]:
    return frozenset(fold(text).split())


def split_discogs_title(raw: str) -> tuple[str, str]:
    """Découpe le champ `title` d'une release Discogs en (artiste, album).

    Discogs renvoie « Artiste - Album » ; l'album peut lui-même contenir des
    tirets, on ne coupe donc que sur le premier séparateur.
    """
    if " - " in raw:
        artist, _, album = raw.partition(" - ")
        return artist.strip(), album.strip()
    return "", raw.strip()


def slugify(text: str) -> str:
    """Clé stable pour un genre ou un style (« Drum n Bass » -> « drum-n-bass »)."""
    return _WS_RE.sub("-", fold(text)) or "inconnu"
