"""Pipeline de classification : bibliothèque -> Discogs -> classifications.

La taxonomie brute de Discogs est stockée telle quelle. Retravailler les alias
ou les seuils ne nécessite donc pas de refaire un seul appel réseau : seules
les étapes `plan` et `apply` sont rejouées.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Protocol

from ytmgc.config import Config
from ytmgc.matching import best_match
from ytmgc.models import Classification, MatchStatus, ReleaseCandidate, Track
from ytmgc.sources.discogs import query_key
from ytmgc.sources.lastfm import query_key as lastfm_key
from ytmgc.taxonomy import Taxonomy, load_taxonomy
from ytmgc.store import Repository


class CandidateSource(Protocol):
    def search(self, track: Track, *, limit: int = 10) -> list[ReleaseCandidate]: ...


class TagSource(Protocol):
    def top_tags(self, track: Track) -> list[tuple[str, int]]: ...


#: Tags conservés par titre. Au-delà, ce sont des étiquettes d'usage
#: (« favourites », « 00s ») qui n'apprennent rien sur la musique.
KEPT_TAGS = 10


@dataclass(slots=True)
class ClassifyStats:
    total: int = 0
    matched: int = 0
    review: int = 0
    unmatched: int = 0
    from_cache: int = 0
    api_calls: int = 0
    tagged: int = 0
    refined: int = 0

    def line(self) -> str:
        line = (
            f"{self.total} titre(s) traité(s) : {self.matched} classé(s), "
            f"{self.review} à vérifier, {self.unmatched} sans correspondance "
            f"({self.api_calls} appel(s) Discogs, {self.from_cache} depuis le cache)"
        )
        if self.tagged:
            line += f" — {self.tagged} titre(s) documentés par Last.fm, {self.refined} affinés"
        return line


def classify_tracks(
    tracks: list[Track],
    repository: Repository,
    source: CandidateSource,
    config: Config,
    *,
    progress: Callable[[Track, Classification], None] | None = None,
    tag_source: TagSource | None = None,
    taxonomy: Taxonomy | None = None,
) -> ClassifyStats:
    """Apparie chaque titre, et l'affine par ses propres tags quand on en a.

    Discogs décrit une release : tous les titres d'un album en héritent
    identiquement. Les tags Last.fm, eux, portent sur le morceau — ce sont eux
    qui rattrapent la ballade perdue au milieu d'un disque punk.
    """
    stats = ClassifyStats(total=len(tracks))
    taxonomy = taxonomy if taxonomy is not None else load_taxonomy()

    for track in tracks:
        key = query_key(track)
        candidates = repository.cached_candidates(key, config.discogs.cache_ttl_days)
        if candidates is None:
            candidates = source.search(track, limit=config.matching.max_candidates)
            repository.store_candidates(key, candidates)
            stats.api_calls += 1
        else:
            stats.from_cache += 1

        weighted = _tags_for(track, repository, config, tag_source)
        classification = _classify(track, candidates, config)
        if weighted:
            stats.tagged += 1
            classification = _refine(classification, weighted, config, taxonomy, stats)
        repository.save_classification(classification)

        if classification.status is MatchStatus.MATCHED:
            stats.matched += 1
        elif classification.status is MatchStatus.REVIEW:
            stats.review += 1
        else:
            stats.unmatched += 1

        if progress is not None:
            progress(track, classification)

    return stats


def _tags_for(
    track: Track,
    repository: Repository,
    config: Config,
    tag_source: TagSource | None,
) -> list[tuple[str, int]]:
    """Tags du titre, depuis le cache ou depuis Last.fm.

    Un titre inconnu de Last.fm renvoie une liste vide, mise en cache comme
    les autres : l'absence de tags est un résultat, et il ne sert à rien de le
    redemander à chaque analyse.
    """
    if tag_source is None or not config.lastfm.enabled:
        return []

    key = lastfm_key(track)
    cached = repository.cached_tags(key, config.lastfm.cache_ttl_days)
    if cached is not None:
        return cached

    try:
        tags = tag_source.top_tags(track)
    except Exception:  # noqa: BLE001 - un tag manquant ne doit pas stopper l'analyse
        return []
    repository.store_tags(key, tags)
    return tags


def _refine(
    classification: Classification,
    weighted: list[tuple[str, int]],
    config: Config,
    taxonomy: Taxonomy,
    stats: ClassifyStats,
) -> Classification:
    """Remplace les styles de la release par ceux que porte le titre lui-même.

    Seuls les tags nommant un style connu sont retenus : les tags libres
    (« 00s », « seen live ») sont écartés par construction, sans liste noire à
    tenir. À défaut de style reconnu, les styles de la release demeurent.
    """
    kept = tuple(name for name, _ in weighted[:KEPT_TAGS])
    # L'ambiance se décide ici : le planner ne verra plus les poids, et ce sont
    # eux qui départagent des tags d'humeur presque tous faiblement pondérés.
    mood = taxonomy.mood_from_tags(weighted, minimum=config.lastfm.min_mood_weight)

    refined: list[str] = []
    for name, weight in weighted:
        if weight < config.lastfm.min_tag_weight:
            continue
        style = taxonomy.style_from_tag(name)
        if style and style not in refined:
            refined.append(style)
        if len(refined) >= config.lastfm.max_styles_per_track:
            break

    if not refined:
        return replace(classification, tags=kept, mood=mood)

    stats.refined += 1
    return replace(classification, styles=tuple(refined), tags=kept, mood=mood)


def _classify(track: Track, candidates: list[ReleaseCandidate], config: Config) -> Classification:
    match = best_match(track, candidates)
    if match is None:
        return Classification(track.video_id, MatchStatus.UNMATCHED)

    if match.score >= config.matching.min_score:
        status = MatchStatus.MATCHED
    elif match.score >= config.matching.review_score:
        # Correspondance plausible mais incertaine : conservée pour inspection
        # (`ytmgc review`) sans alimenter les playlists.
        status = MatchStatus.REVIEW
    else:
        return Classification(track.video_id, MatchStatus.UNMATCHED, score=match.score)

    return Classification(
        video_id=track.video_id,
        status=status,
        discogs_id=match.candidate.discogs_id,
        score=match.score,
        genres=match.candidate.genres,
        styles=match.candidate.styles,
        year=match.candidate.year,
    )
