"""Aperçu : à quoi ressemblera la bibliothèque si l'on applique les changements.

Cette couche ne parle ni de HTTP ni de terminal. Elle assemble ce que le
planner a décidé, ce que YouTube Music contient déjà et les titres réellement
concernés, sous une forme directement affichable — et sérialisable en JSON pour
l'interface web.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ytmgc.config import Config
from ytmgc.models import MatchStatus, Op, PlaylistPlan, RemotePlaylist, SyncAction, Track
from ytmgc.planner import plan_playlists
from ytmgc.store import Repository
from ytmgc.sync import diff, managed_by_key
from ytmgc.taxonomy import load_taxonomy

#: Nombre de titres montrés en exemple par playlist.
SAMPLE_SIZE = 6


@dataclass(frozen=True, slots=True)
class PlaylistPreview:
    key: str
    name: str
    description: str
    kind: str
    count: int
    #: "création", "mise à jour" ou "inchangée".
    change: str
    added: int
    removed: int
    sample: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class LibraryPreview:
    sort_mode: str
    playlists: list[PlaylistPreview]
    #: Playlists gérées qui n'ont plus lieu d'être avec ce tri.
    obsolete: list[str] = field(default_factory=list)
    total_tracks: int = 0
    classified: int = 0
    review: int = 0
    unmatched: int = 0
    unclassified: int = 0
    #: Somme des affectations ; supérieure au nombre de titres en mode "all".
    assignments: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _sample(video_ids: tuple[str, ...], tracks: dict[str, Track]) -> list[str]:
    labels = []
    for video_id in video_ids[:SAMPLE_SIZE]:
        track = tracks.get(video_id)
        if track is not None:
            labels.append(track.label())
    return labels


def build_preview(
    repository: Repository,
    config: Config,
    *,
    sort_mode: str,
    remote: list[RemotePlaylist] | None = None,
) -> tuple[LibraryPreview, list[PlaylistPlan], list[SyncAction]]:
    """Construit l'aperçu, et renvoie aussi de quoi appliquer sans recalculer.

    `remote` vaut None quand l'état distant n'a pas encore été lu : l'aperçu
    présente alors tout comme une création, ce qui reste exact pour une
    première utilisation.
    """
    tracks = {track.video_id: track for track in repository.all_tracks()}
    classifications = repository.classifications()
    plans = plan_playlists(classifications, load_taxonomy(), config)

    existing = managed_by_key(remote or [], config.sync.marker)
    actions = diff(plans, existing, config)

    added_by_key: dict[str, int] = {}
    removed_by_key: dict[str, int] = {}
    created_keys: set[str] = set()
    for action in actions:
        if action.op is Op.CREATE:
            created_keys.add(action.key)
            added_by_key[action.key] = len(action.video_ids)
        elif action.op is Op.ADD:
            added_by_key[action.key] = added_by_key.get(action.key, 0) + len(action.video_ids)
        elif action.op is Op.REMOVE:
            removed_by_key[action.key] = removed_by_key.get(action.key, 0) + len(action.video_ids)

    previews: list[PlaylistPreview] = []
    for plan in plans:
        added = added_by_key.get(plan.key, 0)
        removed = removed_by_key.get(plan.key, 0)
        if plan.key in created_keys:
            change = "création"
        elif added or removed:
            change = "mise à jour"
        else:
            change = "inchangée"
        previews.append(
            PlaylistPreview(
                key=plan.key,
                name=plan.name,
                description=plan.description,
                kind=plan.kind,
                count=len(plan.video_ids),
                change=change,
                added=added,
                removed=removed,
                sample=_sample(plan.video_ids, tracks),
            )
        )

    planned_keys = {plan.key for plan in plans}
    obsolete = sorted(
        playlist.title for key, playlist in existing.items() if key not in planned_keys
    )

    counts = repository.counts_by_status()
    summary = LibraryPreview(
        sort_mode=sort_mode,
        playlists=previews,
        obsolete=obsolete,
        total_tracks=len(tracks),
        classified=counts.get(MatchStatus.MATCHED.value, 0),
        review=counts.get(MatchStatus.REVIEW.value, 0),
        unmatched=counts.get(MatchStatus.UNMATCHED.value, 0),
        unclassified=len(repository.unclassified_tracks()),
        assignments=sum(len(plan.video_ids) for plan in plans),
        created=sum(1 for p in previews if p.change == "création"),
        updated=sum(1 for p in previews if p.change == "mise à jour"),
        unchanged=sum(1 for p in previews if p.change == "inchangée"),
    )
    return summary, plans, actions
