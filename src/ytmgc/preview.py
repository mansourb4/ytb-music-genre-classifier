"""Aperçu : à quoi ressemblera la bibliothèque si l'on applique les changements.

Cette couche ne parle ni de HTTP ni de terminal. Elle assemble ce que le
planner a décidé, ce que YouTube Music contient déjà et les titres réellement
concernés, sous une forme directement affichable — et sérialisable en JSON pour
l'interface web.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Mapping

from ytmgc.config import Config
from ytmgc.models import (
    Classification,
    MatchStatus,
    Op,
    PlaylistPlan,
    RemotePlaylist,
    SyncAction,
    Track,
)
from ytmgc.planner import MAX_GATHERED_STYLES, NOTES, plan_playlists
from ytmgc.store import Repository
from ytmgc.sync import diff, managed_by_key
from ytmgc.taxonomy import load_taxonomy

@dataclass(frozen=True, slots=True)
class TrackPreview:
    """Un titre tel qu'affiché dans le détail d'une playlist."""

    video_id: str
    title: str
    artist: str
    album: str | None
    thumbnail: str | None
    genres: list[str]
    styles: list[str]
    year: int | None
    #: Tags Last.fm du titre : ce sont eux qui expliquent un classement
    #: différent de celui de son album.
    tags: list[str] = field(default_factory=list)


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
    #: Pochette du premier titre : les playlists prévues n'existent pas encore,
    #: elles n'ont donc pas d'image propre.
    image: str | None = None
    #: Description décomposée, pour un affichage lisible plutôt qu'un bloc brut.
    #: "style" (genre et style Discogs) ou "mood" (ambiance déduite).
    axis: str = "style"
    genre: str | None = None
    style: str | None = None
    genre_text: str | None = None
    style_text: str | None = None
    #: Styles réunis sous une ambiance : ce qui rend la déduction vérifiable.
    gathered: list[str] = field(default_factory=list)
    note: str | None = None
    tracks: list[TrackPreview] = field(default_factory=list)


#: Pourquoi un titre n'entre dans aucune playlist. Chaque cause appelle un
#: geste différent, et les confondre laisse l'utilisateur devant un total
#: inexpliqué.
REASONS = {
    "unscanned": "jamais analysé",
    "unmatched": "aucune correspondance Discogs",
    "review": "appariement trop incertain",
    "no_genre": "release sans genre exploitable",
    "no_tags": "ni tag Last.fm ni style Discogs",
    "no_mood": "aucune ambiance déterminable",
}


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
    #: Titres qui n'entreront dans aucune playlist, avec leur cause.
    unsorted: list[dict] = field(default_factory=list)
    unsorted_by_reason: dict[str, int] = field(default_factory=dict)
    #: Libellés des causes, pour que l'interface n'ait pas à les redire.
    reason_labels: dict[str, str] = field(default_factory=lambda: dict(REASONS))

    def to_dict(self) -> dict:
        return asdict(self)


def filter_plans(
    plans: list[PlaylistPlan],
    excluded_playlists: Iterable[str] = (),
    excluded_tracks: Mapping[str, Iterable[str]] | None = None,
) -> list[PlaylistPlan]:
    """Retire du plan ce que l'utilisateur a décoché dans l'aperçu.

    Une playlist vidée de tous ses titres disparaît du plan : la créer vide
    n'aurait aucun sens.
    """
    excluded = set(excluded_playlists)
    per_playlist = {key: set(values) for key, values in (excluded_tracks or {}).items()}

    kept: list[PlaylistPlan] = []
    for plan in plans:
        if plan.key in excluded:
            continue
        dropped = per_playlist.get(plan.key, set())
        video_ids = tuple(v for v in plan.video_ids if v not in dropped)
        if not video_ids:
            continue
        kept.append(plan if video_ids == plan.video_ids else replace(plan, video_ids=video_ids))
    return kept


def _detail(
    video_ids: tuple[str, ...],
    tracks: dict[str, Track],
    classifications: dict[str, Classification],
) -> list[TrackPreview]:
    """Détail de chaque titre d'une playlist, taxonomie Discogs comprise."""
    detailed: list[TrackPreview] = []
    for video_id in video_ids:
        track = tracks.get(video_id)
        if track is None:
            continue
        classification = classifications.get(video_id)
        detailed.append(
            TrackPreview(
                video_id=video_id,
                title=track.title,
                artist=", ".join(track.artists),
                album=track.album,
                thumbnail=track.thumbnail,
                genres=list(classification.genres) if classification else [],
                styles=list(classification.styles) if classification else [],
                year=classification.year if classification else None,
                tags=list(classification.tags) if classification else [],
            )
        )
    return detailed


def _reason(classification: Classification | None, by_mood: bool) -> str:
    """Cause du non-rangement d'un titre.

    Plusieurs filtres successifs écartent des titres, et aucun n'est visible
    dans la bibliothèque finale : il faut donc les nommer ici.
    """
    if classification is None:
        return "unscanned"
    if by_mood:
        # En mode ambiance, Discogs n'est plus qu'un appoint : ce qui manque
        # est soit toute matière, soit une ambiance tirée de cette matière.
        if not classification.tags and not classification.styles:
            return "no_tags"
        return "no_mood"
    if classification.status is MatchStatus.UNMATCHED:
        return "unmatched"
    if classification.status is MatchStatus.REVIEW:
        return "review"
    return "no_genre"


def _unsorted(
    tracks: dict[str, Track],
    classifications: dict[str, Classification],
    placed: set[str],
    config: Config,
) -> list[tuple[Track, str]]:
    by_mood = config.taxonomy.axis == "mood"
    return [
        (track, _reason(classifications.get(video_id), by_mood))
        for video_id, track in tracks.items()
        if video_id not in placed
    ]


def _count_reasons(unsorted: list[tuple[Track, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _track, reason in unsorted:
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _image(video_ids: tuple[str, ...], tracks: dict[str, Track]) -> str | None:
    for video_id in video_ids:
        track = tracks.get(video_id)
        if track is not None and track.thumbnail:
            return track.thumbnail
    return None


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
    by_video = {item.video_id: item for item in classifications}
    taxonomy = load_taxonomy()
    plans = plan_playlists(classifications, taxonomy, config)

    # La clé n'étant plus dans la description, elle vient de la base — et à
    # défaut du nom attendu, calculé depuis le plan qu'on vient d'établir.
    known = {
        playlist_id: key for key, (playlist_id, _) in repository.managed_playlists().items()
    }
    existing = managed_by_key(
        remote or [],
        config.sync.marker,
        known=known,
        names={plan.name: plan.key for plan in plans},
    )
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
                image=_image(plan.video_ids, tracks),
                axis=config.taxonomy.axis,
                genre=plan.genre,
                style=plan.style,
                genre_text=(
                    None
                    if config.taxonomy.axis == "mood"
                    else taxonomy.describe_genre(plan.genre) if plan.genre else None
                ),
                style_text=(
                    (taxonomy.describe_mood(plan.style) if config.taxonomy.axis == "mood"
                     else taxonomy.describe_style(plan.style))
                    if plan.style else None
                ),
                gathered=(
                    taxonomy.styles_of_mood(plan.style)[:MAX_GATHERED_STYLES]
                    if config.taxonomy.axis == "mood" and plan.style
                    else []
                ),
                note=NOTES.get(plan.kind),
                tracks=_detail(plan.video_ids, tracks, by_video),
            )
        )

    placed = {video_id for plan in plans for video_id in plan.video_ids}
    unsorted = _unsorted(tracks, by_video, placed, config)

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
        unsorted=[
            {"video_id": track.video_id, "label": track.label(),
             "thumbnail": track.thumbnail, "reason": reason}
            for track, reason in unsorted
        ],
        unsorted_by_reason=_count_reasons(unsorted),
        created=sum(1 for p in previews if p.change == "création"),
        updated=sum(1 for p in previews if p.change == "mise à jour"),
        unchanged=sum(1 for p in previews if p.change == "inchangée"),
    )
    return summary, plans, actions
