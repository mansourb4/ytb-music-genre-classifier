"""Aperçu : à quoi ressemblera la bibliothèque si l'on applique les changements.

Cette couche ne parle ni de HTTP ni de terminal. Elle assemble ce que le
planner a décidé, ce que YouTube Music contient déjà et les titres réellement
concernés, sous une forme directement affichable — et sérialisable en JSON pour
l'interface web.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Mapping

from ytmgc import origins, placements, verdicts
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
from ytmgc.playlist_plan import Plan, load_plan, usable
from ytmgc.store import Repository
from ytmgc.sync import diff, managed_by_key
from ytmgc.taxonomy import load_taxonomy
from ytmgc.verdicts import VerdictBook, track_key

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
    mood: str | None = None
    #: Ce que le verdict dit du morceau, et avec quelle assurance.
    note: str = ""
    confidence: float | None = None
    unsure: bool = False
    #: Placé ici à la main, et non par les règles.
    moved: bool = False
    #: Artiste principal : c'est par lui que la relecture regroupe les titres.
    main_artist: str = ""
    #: Autres playlists où le titre aurait sa place, de la plus proche à la
    #: moins proche : un clic suffit pour l'y déplacer.
    suggestions: list[dict] = field(default_factory=list)


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
    #: Tri « Par famille » : les règles d'entrée, et l'écart à la taille visée.
    criteria: list[str] = field(default_factory=list)
    size_warning: str | None = None
    #: Titres dont le verdict est peu sûr.
    unsure: int = 0
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
    "no_rule": "aucune playlist du plan ne l'accepte",
    "kept_out": "tenu hors des playlists à la main",
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
    #: Tri « Par famille » : les playlists où un titre peut être déplacé —
    #: toutes celles du plan, même vides.
    targets: list[dict] = field(default_factory=list)
    #: Titres déplacés à la main, et playlists visées qui n'existent plus.
    moved: int = 0
    stale_placements: list[str] = field(default_factory=list)
    #: Titres rangés sur un verdict peu sûr, ni validés ni déplacés.
    to_review: int = 0
    #: Artistes nommés par le plan sans titre dans la bibliothèque.
    unknown_artists: list[str] = field(default_factory=list)

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
    book: VerdictBook,
    unsure_below: float,
    moved: set[str] = frozenset(),
    suggestions: dict[str, list[dict]] | None = None,
) -> list[TrackPreview]:
    """Détail de chaque titre d'une playlist, taxonomie et verdict compris."""
    detailed: list[TrackPreview] = []
    for video_id in video_ids:
        track = tracks.get(video_id)
        if track is None:
            continue
        classification = classifications.get(video_id)
        verdict = book.get(track_key(track))
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
                mood=classification.mood if classification else None,
                note=verdict.note if verdict else "",
                confidence=verdict.confidence if verdict else None,
                # Un titre validé (verdict passé en « manuel ») ou déplacé a été
                # relu : il n'est plus à vérifier, quelle que soit la confiance.
                unsure=(
                    verdict is not None
                    and verdict.source != verdicts.MANUAL
                    and verdict.confidence < unsure_below
                    and video_id not in moved
                ),
                moved=video_id in moved,
                main_artist=track.artist,
                suggestions=(suggestions or {}).get(video_id, []),
            )
        )
    return detailed


#: Nombre de playlists proposées par titre, et proximité minimale pour l'être.
SUGGESTED = 3
MIN_AFFINITY = 0.15


@dataclass(frozen=True, slots=True)
class _Profile:
    """Ce qu'on compare d'un titre à une playlist."""

    artist: str
    styles: frozenset[str]
    mood: str | None
    genres: frozenset[str]
    countries: frozenset[str] = frozenset()


def _profile(
    track: Track, classification: Classification | None, taxonomy, origins=None
) -> _Profile:
    from ytmgc.matching.normalize import fold, normalize_artist
    from ytmgc.origins import countries_of

    styles = (taxonomy.canonical_style(s) for s in (classification.styles if classification else ()))
    return _Profile(
        artist=normalize_artist(track.artist),
        styles=frozenset(fold(s) for s in styles if s),
        mood=classification.mood if classification else None,
        genres=frozenset(
            fold(taxonomy.display_genre(g)) for g in (classification.genres if classification else ())
        ),
        countries=frozenset(countries_of(track.artists, origins or {})),
    )


def suggest_playlists(
    plans: list[PlaylistPlan],
    profiles: dict[str, _Profile],
    plan: Plan,
) -> dict[str, list[dict]]:
    """Les playlists voisines de chaque titre, d'après ce qu'elles contiennent.

    Une règle dit où un titre *doit* aller ; elle ne dit pas où il pourrait
    aussi aller. Ce qu'une playlist contient le dit mieux : on y cherche les
    autres titres du même artiste, puis le même style, la même ambiance et le
    même genre. Une playlist fourre-tout d'une famille, majoritairement calme,
    n'est ainsi pas proposée pour un titre énergique.

    Il faut un lien réel — artiste, style ou genre — pour être proposé :
    l'ambiance seule rapprocherait Interstellar d'une transe gnaoua. Et le pays
    d'un artiste, quand on le connaît, est respecté : Nekfeu n'est pas proposé
    pour le rap marocain.
    """
    targets = [(p.key, p.name) for p in plan.playlists]
    # Pays exigés par chaque playlist ; None si l'une de ses règles n'en exige pas.
    required = {
        p.key: (frozenset(c for r in p.rules for c in r.countries)
                if p.rules and all(r.countries for r in p.rules) else None)
        for p in plan.playlists
    }
    members = {plan.key: plan.video_ids for plan in plans}
    by_artist: Counter[str] = Counter(p.artist for p in profiles.values())
    stats = {}
    for key, video_ids in members.items():
        known = [profiles[v] for v in video_ids if v in profiles]
        if not known:
            continue
        stats[key] = (
            len(known),
            Counter(p.artist for p in known),
            Counter(s for p in known for s in p.styles),
            Counter(p.mood for p in known if p.mood),
            Counter(g for p in known for g in p.genres),
        )
    current = {v: key for key, video_ids in members.items() for v in video_ids}

    suggestions: dict[str, list[dict]] = {}
    for video_id, profile in profiles.items():
        here = current.get(video_id)
        others = by_artist[profile.artist] - 1
        scored = []
        for order, (key, name) in enumerate(targets):
            if key == here or key not in stats:
                continue
            wanted = required[key]
            # Une playlist d'attente (`pays = ["?"]`) n'a rien à offrir à un
            # titre dont le pays est connu.
            if wanted is not None and profile.countries and not profile.countries & wanted:
                continue
            size, artists, styles, moods, genres = stats[key]
            same_artist = artists[profile.artist] / others if others > 0 else 0.0
            link = (
                0.6 * min(same_artist, 1.0)
                + 0.4 * max((styles[s] / size for s in profile.styles), default=0.0)
                + 0.2 * max((genres[g] / size for g in profile.genres), default=0.0)
            )
            if link == 0:
                continue
            score = link + 0.3 * (moods[profile.mood] / size if profile.mood else 0.0)
            if score >= MIN_AFFINITY:
                scored.append((-score, order, key, name))
        # Pays inconnu : la vraie question est le pays, pas l'ambiance. On
        # propose donc une playlist par pays (la plus proche), plutôt que trois
        # variantes d'un même pays.
        chosen, countries_seen = [], set()
        for _, _, key, name in sorted(scored):
            wanted = required[key]
            if not profile.countries and wanted is not None:
                if wanted in countries_seen:
                    continue
                countries_seen.add(wanted)
            chosen.append({"key": key, "name": name})
            if len(chosen) == SUGGESTED:
                break
        suggestions[video_id] = chosen
    return suggestions


def _reason(classification: Classification | None, by_mood: bool, by_plan: bool = False) -> str:
    """Cause du non-rangement d'un titre.

    Plusieurs filtres successifs écartent des titres, et aucun n'est visible
    dans la bibliothèque finale : il faut donc les nommer ici.
    """
    if classification is None:
        return "unscanned"
    if by_plan and usable(classification):
        # Le morceau est décrit : c'est le plan qui n'a pas prévu sa place.
        return "no_rule"
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
    kept_out: set[str] = frozenset(),
) -> list[tuple[Track, str]]:
    by_mood = config.taxonomy.axis == "mood"
    by_plan = config.taxonomy.axis == "plan"
    return [
        (track, "kept_out" if video_id in kept_out
         else _reason(classifications.get(video_id), by_mood, by_plan))
        for video_id, track in tracks.items()
        if video_id not in placed
    ]


def _described(classification: Classification | None) -> str:
    """Genre, style et ambiance d'un morceau : de quoi écrire la règle qui manque."""
    if classification is None:
        return ""
    parts = [" / ".join((*classification.genres, *classification.styles))]
    if classification.mood:
        parts.append(classification.mood)
    return " · ".join(part for part in parts if part)


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
    taxonomy = load_taxonomy()
    book = verdicts.load(config.claude.verdicts_file) if config.claude.enabled else VerdictBook()
    if len(book):
        # Le fichier fait autorité : une ligne corrigée à la main se voit
        # dès l'aperçu suivant, sans repasser par l'analyse.
        from ytmgc.enrich import apply_verdicts

        apply_verdicts(repository, book, taxonomy, config)
    classifications = repository.classifications()
    by_video = {item.video_id: item for item in classifications}
    by_plan = config.taxonomy.axis == "plan"
    plan: Plan | None = load_plan(config.taxonomy.playlists_file, taxonomy) if by_plan else None
    planned = plan.by_key() if plan else {}
    # Les déplacements ne valent que pour le plan : ailleurs, pas de playlist fixe.
    wished = (
        placements.resolve(list(tracks.values()), placements.load(config.taxonomy.placements_file))
        if by_plan else {}
    )
    honoured = {v: p for v, p in wished.items() if p.nowhere or p.target in planned}
    stale = sorted({p.playlist for p in wished.values() if p.target and p.target not in planned})
    plans = plan_playlists(
        classifications, taxonomy, config, placements.overrides(honoured),
        artists={video_id: track.artists for video_id, track in tracks.items()},
    )

    # La clé n'étant plus dans la description, elle vient de la base — et à
    # défaut du nom attendu, calculé depuis le plan qu'on vient d'établir.
    known = {
        playlist_id: key for key, (playlist_id, _) in repository.managed_playlists().items()
    }
    existing = managed_by_key(
        remote or [],
        config.sync.marker,
        known=known,
        names={item.name: item.key for item in plans},
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

    known_origins = origins.load(config.taxonomy.origins_file) if plan else {}
    suggested = (
        suggest_playlists(
            plans,
            {v: _profile(t, by_video.get(v), taxonomy, known_origins) for v, t in tracks.items()},
            plan,
        )
        if plan else {}
    )

    previews: list[PlaylistPreview] = []
    for item in plans:
        added = added_by_key.get(item.key, 0)
        removed = removed_by_key.get(item.key, 0)
        if item.key in created_keys:
            change = "création"
        elif added or removed:
            change = "mise à jour"
        else:
            change = "inchangée"
        detail = _detail(item.video_ids, tracks, by_video, book, config.claude.unsure_below,
                         moved=set(honoured), suggestions=suggested)
        written = planned.get(item.key)
        previews.append(
            PlaylistPreview(
                key=item.key,
                name=item.name,
                description=item.description,
                kind=item.kind,
                count=len(item.video_ids),
                change=change,
                added=added,
                removed=removed,
                image=_image(item.video_ids, tracks),
                axis=config.taxonomy.axis,
                genre=item.genre,
                style=item.style,
                genre_text=(
                    None
                    if config.taxonomy.axis == "mood"
                    else taxonomy.describe_genre(item.genre) if item.genre else None
                ),
                style_text=(
                    (taxonomy.describe_mood(item.style) if config.taxonomy.axis == "mood"
                     else taxonomy.describe_style(item.style))
                    if item.style else None
                ),
                gathered=(
                    taxonomy.styles_of_mood(item.style)[:MAX_GATHERED_STYLES]
                    if config.taxonomy.axis == "mood" and item.style
                    else []
                ),
                note=(written.description or None) if written else NOTES.get(item.kind),
                criteria=[rule.describe() for rule in written.rules] if written else [],
                size_warning=plan.size_warning(len(item.video_ids)) if plan else None,
                unsure=sum(1 for track in detail if track.unsure),
                tracks=detail,
            )
        )

    placed = {video_id for item in plans for video_id in item.video_ids}
    unsorted = _unsorted(tracks, by_video, placed, config,
                         kept_out={v for v, p in honoured.items() if p.nowhere})

    planned_keys = {item.key for item in plans}
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
        assignments=sum(len(item.video_ids) for item in plans),
        unsorted=[
            {"video_id": track.video_id, "label": track.label(),
             "thumbnail": track.thumbnail, "reason": reason,
             "detail": _described(by_video.get(track.video_id)) if reason == "no_rule" else "",
             "suggestions": suggested.get(track.video_id, [])}
            for track, reason in unsorted
        ],
        unsorted_by_reason=_count_reasons(unsorted),
        targets=[{"key": p.key, "name": p.name} for p in plan.playlists] if plan else [],
        moved=len(honoured),
        stale_placements=stale,
        to_review=sum(p.unsure for p in previews),
        unknown_artists=(
            plan.unknown_artists([track.artists for track in tracks.values()]) if plan else []
        ),
        created=sum(1 for p in previews if p.change == "création"),
        updated=sum(1 for p in previews if p.change == "mise à jour"),
        unchanged=sum(1 for p in previews if p.change == "inchangée"),
    )
    return summary, plans, actions
