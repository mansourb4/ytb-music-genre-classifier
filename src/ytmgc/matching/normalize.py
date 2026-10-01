"""Normalisation des libellés avant comparaison.

Les titres YouTube Music portent beaucoup de bruit éditorial ("(Official Video)",
"feat. X", "- Remastered 2011") absent des métadonnées Discogs. On le retire
avant tout calcul de similarité.
"""

from __future__ import annotations

import re
import unicodedata

#: Segments parenthésés ou suffixés à supprimer d'un titre. Ils disent comment
#: le morceau a été publié, jamais ce qu'il est : deux titres qui ne diffèrent
#: que par eux sont le même morceau.
_NOISE_PATTERNS = (
    r"official\s+(hd\s+)?(music\s+)?(video|audio|lyric\s+video|visuali[sz]er)",
    r"(clip|vid[ée]o|audio)\s+officiel(le)?",
    r"video\s*clip",
    r"visuali[sz]er",
    r"lyrics?(\s+video)?",
    r"paroles",
    r"audio\s+only",
    r"hd|hq|4k",
    r"remaster(ed)?(\s+\d{4})?",
    r"\d{4}\s+(digital\s+)?remaster(ed)?",
    r"(deluxe|expanded|anniversary|special)\s+(edition|version)",
    r"(single|album|mono|stereo)\s+(version|edit)",
    r"mono|stereo",
    r"radio\s+edit",
    r"explicit|clean",
    r"free\s+download",
)
_NOISE_RE = re.compile(
    r"\b(?:" + r"|".join(f"(?:{p})" for p in _NOISE_PATTERNS) + r")\b", re.IGNORECASE
)

#: Mots qui, restés seuls une fois le bruit retiré, n'en sont que le reliquat :
#: « (Official HD Video) » laisse « official video », « - Remastered Version »
#: laisse « version ». Un mot qui décrit la musique — live, acoustic, remix —
#: n'y figure pas, et suffit à conserver le segment.
_FILLER = frozenset({"official", "officiel", "officielle", "version", "video", "audio", "clip"})

#: Suffixe « Titre - Remastered 2011 » : le dernier tiret, entouré d'espaces.
_SUFFIX_RE = re.compile(r"^(?P<head>.*\S)\s+[-–—]\s+(?P<tail>[^-–—]+)$")

#: Chaînes YouTube qui portent le nom d'un artiste sans être lui : « NirvanaVEVO »,
#: « Nirvana - Topic ». Le même morceau y apparaît sous trois « artistes ».
_CHANNEL_RE = re.compile(r"(?:\s*-\s*topic|vevo)\s*$", re.IGNORECASE)

#: « Artiste - Titre » dans le titre lui-même, comme le font les clips.
_PREFIXED_RE = re.compile(r"^\s*(?P<artist>.+?)\s+[-–—:]\s+(?P<title>.+)$")

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


def _is_noise(segment: str) -> bool:
    """Le segment n'est-il fait que de bruit éditorial ?"""
    residue = fold(_NOISE_RE.sub(" ", segment))
    return all(word in _FILLER for word in residue.split())


def strip_noise(title: str) -> str:
    """Retire les mentions éditoriales d'un titre de piste.

    Un segment entre parenthèses — ou suffixé par un tiret — n'est supprimé que
    s'il est *entièrement* du bruit : « (Deep House Mix) » ou « (Live at
    Wembley) » restent, car ils distinguent des versions différentes.
    """
    cleaned = _BRACKETED_RE.sub(
        lambda match: "" if _is_noise(match.group(1)) else match.group(0), title
    )
    cleaned = _WS_RE.sub(" ", cleaned).strip(" -–—")
    # « Titre - Remastered 2011 - Mono » : on retire tant que le dernier
    # segment n'est que du bruit, et on s'arrête au premier qui dit quelque
    # chose de la musique.
    while (match := _SUFFIX_RE.match(cleaned)) and _is_noise(match.group("tail")):
        cleaned = match.group("head").strip(" -–—")
    return cleaned


def normalize_title(title: str) -> str:
    """Titre prêt à comparer : bruit retiré, featurings retirés, replié."""
    return fold(_FEAT_RE.sub("", strip_noise(title)))


def normalize_artist(artist: str) -> str:
    """Nom d'artiste replié, sans suffixe d'homonyme Discogs, sans suffixe de
    chaîne YouTube (« VEVO », « - Topic ») ni « The » initial."""
    cleaned = _DISCOGS_HOMONYM_RE.sub("", _CHANNEL_RE.sub("", artist))
    folded = fold(_FEAT_RE.sub("", cleaned))
    return folded[4:] if folded.startswith("the ") else folded


def song_key(artist: str, title: str) -> str:
    """Identité d'un morceau, quelle que soit la façon dont il a été publié.

    YouTube Music présente un même morceau sous plusieurs formes : le titre de
    l'album, le clip (« Nirvana - Smells Like Teen Spirit (Official Video) »),
    la version de la chaîne « Nirvana - Topic » ou « NirvanaVEVO ». Toutes
    donnent la même clé. Seul compte l'artiste principal : les invités
    varient d'une publication à l'autre, le morceau non.
    """
    main = normalize_artist(artist)
    # « Artiste - Titre » dans le champ titre : le préfixe n'est retiré que
    # s'il nomme bien l'artiste du titre — « Song 2 - Live » doit rester tel.
    prefixed = _PREFIXED_RE.match(strip_noise(title))
    if prefixed and main and normalize_artist(prefixed.group("artist")) == main:
        title = prefixed.group("title")
    return f"{main}::{normalize_title(title)}"


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
