"""Scoring des candidats Discogs pour un titre donné.

Le score final est une moyenne pondérée de composantes indépendantes, chacune
dans [0, 1]. Le détail est conservé (`ScoredCandidate.breakdown`) pour pouvoir
expliquer — et déboguer — un appariement douteux.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Iterable

from ytmgc.matching.normalize import (
    normalize_artist,
    normalize_title,
    split_artists,
    tokens,
)
from ytmgc.models import ReleaseCandidate, ScoredCandidate, Track

#: Poids des composantes. L'artiste prime : deux morceaux homonymes d'artistes
#: différents relèvent presque toujours de styles différents.
WEIGHTS = {
    "artist": 0.50,
    "release": 0.40,
    "metadata": 0.10,
}

#: Similarité en deçà de laquelle on considère qu'il n'y a *aucun* signal.
#: Deux chaînes courtes sans rapport ("Moon Safari" / "Talkie Walkie") obtiennent
#: naturellement ~0.3 avec SequenceMatcher ; sans ce seuil, ce bruit gonfle tous
#: les scores et fait passer des appariements faux au-dessus du seuil d'acceptation.
CONFIDENCE_FLOOR = 0.5


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _confident(value: float, floor: float = CONFIDENCE_FLOOR) -> float:
    """Réétale [floor, 1] sur [0, 1] et annule tout ce qui est sous le seuil."""
    return 0.0 if value < floor else (value - floor) / (1.0 - floor)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _artist_score(track: Track, candidate: ReleaseCandidate) -> float:
    """Meilleure correspondance entre un artiste du titre et l'artiste Discogs.

    On prend le maximum plutôt que la moyenne : sur un morceau collaboratif,
    Discogs ne crédite souvent qu'un seul des artistes listés par YouTube.
    """
    track_artists = split_artists(track.artists)
    candidate_artists = split_artists(candidate.artist) if candidate.artist else ()
    if not track_artists or not candidate_artists:
        return 0.0
    best = 0.0
    for left in track_artists:
        for right in candidate_artists:
            if left == right:
                return 1.0
            best = max(best, _ratio(left, right))
    return _confident(best)


def _release_score(track: Track, candidate: ReleaseCandidate) -> float:
    """Le titre Discogs désigne une *release*, celui de YouTube une *piste*.

    Deux situations distinctes :

    * le titre porte un album — on le compare directement à la release, c'est le
      signal le plus fiable dont on dispose ;
    * le titre n'a pas d'album (cas fréquent des « J'aime ») — la recherche
      Discogs a alors été faite par piste, donc un titre de release différent
      est normal et ne prouve rien. On part d'une valeur neutre, relevée si la
      release est éponyme du morceau (singles, albums-titres).
    """
    release_title = normalize_title(candidate.title)
    if not release_title:
        return 0.0

    if track.album:
        return _confident(_ratio(normalize_title(track.album), release_title))

    track_title = normalize_title(track.title)
    direct = max(_ratio(track_title, release_title), _jaccard(tokens(track_title), tokens(release_title)))
    return max(0.5, _confident(direct))


def _metadata_score(candidate: ReleaseCandidate) -> float:
    """Qualité intrinsèque du candidat : c'est sa taxonomie qui nous intéresse."""
    score = 0.0
    if candidate.styles:
        score += 0.6
    if candidate.genres:
        score += 0.3
    if candidate.year:
        score += 0.1
    return min(score, 1.0)


def score_candidate(track: Track, candidate: ReleaseCandidate) -> ScoredCandidate:
    breakdown = {
        "artist": _artist_score(track, candidate),
        "release": _release_score(track, candidate),
        "metadata": _metadata_score(candidate),
    }
    total = sum(WEIGHTS[key] * value for key, value in breakdown.items())
    return ScoredCandidate(candidate=candidate, score=round(total, 4), breakdown=breakdown)


def best_match(
    track: Track,
    candidates: Iterable[ReleaseCandidate],
    *,
    require_styles: bool = True,
) -> ScoredCandidate | None:
    """Meilleur candidat, ou None si aucun n'est exploitable.

    `require_styles` écarte les releases sans aucune taxonomie : elles ne
    peuvent alimenter aucune playlist, même correctement appariées.
    """
    scored = [
        score_candidate(track, candidate)
        for candidate in candidates
        if not require_styles or candidate.styles or candidate.genres
    ]
    if not scored:
        return None
    return max(scored, key=lambda item: (item.score, bool(item.candidate.styles)))
