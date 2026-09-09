"""Pipeline de classification : bibliothèque -> Discogs -> classifications.

La taxonomie brute de Discogs est stockée telle quelle. Retravailler les alias
ou les seuils ne nécessite donc pas de refaire un seul appel réseau : seules
les étapes `plan` et `apply` sont rejouées.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ytmgc.config import Config
from ytmgc.matching import best_match
from ytmgc.models import Classification, MatchStatus, ReleaseCandidate, Track
from ytmgc.sources.discogs import query_key
from ytmgc.store import Repository


class CandidateSource(Protocol):
    def search(self, track: Track, *, limit: int = 10) -> list[ReleaseCandidate]: ...


@dataclass(slots=True)
class ClassifyStats:
    total: int = 0
    matched: int = 0
    review: int = 0
    unmatched: int = 0
    from_cache: int = 0
    api_calls: int = 0

    def line(self) -> str:
        return (
            f"{self.total} titre(s) traité(s) : {self.matched} classé(s), "
            f"{self.review} à vérifier, {self.unmatched} sans correspondance "
            f"({self.api_calls} appel(s) Discogs, {self.from_cache} depuis le cache)"
        )


def classify_tracks(
    tracks: list[Track],
    repository: Repository,
    source: CandidateSource,
    config: Config,
    *,
    progress: Callable[[Track, Classification], None] | None = None,
) -> ClassifyStats:
    stats = ClassifyStats(total=len(tracks))

    for track in tracks:
        key = query_key(track)
        candidates = repository.cached_candidates(key, config.discogs.cache_ttl_days)
        if candidates is None:
            candidates = source.search(track, limit=config.matching.max_candidates)
            repository.store_candidates(key, candidates)
            stats.api_calls += 1
        else:
            stats.from_cache += 1

        classification = _classify(track, candidates, config)
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
