"""Planification : des classifications vers l'ensemble de playlists souhaité.

Étapes :
  1. chaque titre classé est converti en couples (genre, style) ;
  2. selon `taxonomy.multi_style`, on ne garde qu'un style ou plusieurs ;
  3. les styles trop peu représentés sont repliés sur leur playlist de genre ;
  4. les genres trop peu représentés sont repliés sur la playlist fourre-tout.

Le repli est ce qui évite l'écueil principal du projet : sans lui, une grosse
bibliothèque produit des centaines de playlists de deux titres, illisibles dans
une interface qui n'a pas de dossiers.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ytmgc.config import Config
from ytmgc.matching.normalize import slugify
from ytmgc.models import Classification, MatchStatus, PlaylistPlan
from ytmgc.taxonomy.rules import GenreStyle, Taxonomy, playlist_name


#: YouTube Music refuse les chevrons dans une description de playlist.
_FORBIDDEN = str.maketrans({"<": "(", ">": ")"})


def build_description(
    marker: str,
    key: str,
    name: str,
    count: int,
    *,
    kind: str,
    item: GenreStyle | None,
    taxonomy: Taxonomy,
) -> str:
    """Description d'une playlist gérée.

    Elle remplit deux rôles distincts :

    * technique — le marqueur autorise l'outil à modifier la playlist, et la
      clé la relie à son couple genre/style même après un renommage manuel ;
    * éditorial — elle définit le genre et le style, pour que la bibliothèque
      se lise comme une cartographie de ce qu'on écoute et pas seulement comme
      un rangement.
    """
    lines = [f"{marker} key={key}", "", f"{name} · {count} titre(s)", ""]

    if item is not None:
        genre_text = taxonomy.describe_genre(item.genre)
        lines.append(f"GENRE — {item.genre}")
        lines.append(genre_text or "Genre Discogs (définition non encore renseignée).")

        if item.style:
            style_text = taxonomy.describe_style(item.style)
            lines.append("")
            lines.append(f"STYLE — {item.style}")
            lines.append(style_text or "Style Discogs (définition non encore renseignée).")

    if kind == "genre":
        lines.append("")
        lines.append(
            "Cette playlist rassemble les titres du genre dont le style est trop "
            "peu représenté dans la bibliothèque pour justifier sa propre playlist."
        )
    elif kind == "fallback":
        lines.append(
            "Cette playlist rassemble les titres des genres trop peu représentés "
            "dans la bibliothèque pour justifier leur propre playlist."
        )

    lines.append("")
    lines.append(
        "Playlist générée automatiquement à partir de la taxonomie Discogs. "
        "Les modifications manuelles seront écrasées au prochain run."
    )
    return "\n".join(lines).translate(_FORBIDDEN)


def _selected_styles(
    resolved: dict[str, tuple[GenreStyle, ...]],
    config: Config,
) -> dict[str, tuple[GenreStyle, ...]]:
    """Applique la politique multi-styles.

    En mode "primary", le style retenu est le plus fréquent dans l'ensemble de
    la bibliothèque : un titre « Deep House / Tech House » rejoint la playlist
    qui existe déjà plutôt que d'en créer une seconde, presque vide.
    """
    if config.taxonomy.multi_style == "all":
        limit = max(1, config.taxonomy.max_styles_per_track)
        return {video_id: items[:limit] for video_id, items in resolved.items()}

    popularity = Counter(item.key for items in resolved.values() for item in items)
    selected = {}
    for video_id, items in resolved.items():
        if items:
            # À égalité de popularité, on garde l'ordre Discogs : le premier
            # style listé est le plus représentatif de la release.
            selected[video_id] = (
                max(items, key=lambda item: (popularity[item.key], -items.index(item))),
            )
    return selected


def _surviving_style(
    item: GenreStyle,
    alternatives: tuple[GenreStyle, ...],
    kept_styles: set[str],
) -> GenreStyle | None:
    """Style retenu pour ce titre, parmi ceux qui ont franchi le seuil.

    Le style élu peut ne pas avoir atteint le seuil alors qu'un *autre* style de
    la même release l'a atteint. Rattraper ce cas évite qu'un titre atterrisse
    dans « Divers » alors qu'une playlist parfaitement pertinente existe déjà.
    """
    if item.style and item.key in kept_styles:
        return item
    for alternative in alternatives:
        if alternative.style and alternative.key in kept_styles:
            return alternative
    return None


def plan_playlists(
    classifications: list[Classification],
    taxonomy: Taxonomy,
    config: Config,
) -> list[PlaylistPlan]:
    """Construit l'état souhaité des playlists. Aucun appel réseau."""
    settings = config.taxonomy

    resolved: dict[str, tuple[GenreStyle, ...]] = {}
    order: list[str] = []
    for classification in classifications:
        if classification.status is not MatchStatus.MATCHED:
            continue
        items = taxonomy.resolve(classification.genres, classification.styles)
        if items:
            resolved[classification.video_id] = items
            order.append(classification.video_id)

    selected = _selected_styles(resolved, config)

    # Passe 1 : effectifs par style, pour décider quels styles survivent.
    style_counts = Counter(
        item.key for items in selected.values() for item in items if item.style
    )
    kept_styles = {
        key for key, count in style_counts.items() if count >= settings.min_tracks_per_style
    }

    # Passe 2 : affectation de chaque titre, avec repli style -> genre.
    members: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, tuple[str, str, GenreStyle]] = {}  # clé -> (nom, type, couple)

    for video_id in order:
        for item in selected.get(video_id, ()):
            surviving = _surviving_style(item, resolved[video_id], kept_styles)
            if surviving is not None:
                key = surviving.key
                name = playlist_name(surviving, style_template=settings.style_name_template,
                                     genre_template=settings.genre_name_template)
                kind = "style"
            else:
                genre_only = GenreStyle(item.genre, None)
                key = genre_only.key
                name = playlist_name(genre_only, style_template=settings.style_name_template,
                                     genre_template=settings.genre_name_template)
                kind = "genre"
            if video_id not in members[key]:
                members[key].append(video_id)
            labels[key] = (name, kind, surviving or GenreStyle(item.genre, None))

    # Passe 3 : repli des genres sous le seuil vers la playlist fourre-tout.
    fallback_key = slugify(settings.fallback_playlist)
    fallback: list[str] = []
    plans: list[PlaylistPlan] = []

    for key, video_ids in members.items():
        name, kind, described = labels[key]
        if kind == "genre" and len(video_ids) < settings.min_tracks_per_genre:
            fallback.extend(v for v in video_ids if v not in fallback)
            continue
        plans.append(
            PlaylistPlan(
                key=key,
                name=name,
                description=build_description(
                    config.sync.marker, key, name, len(video_ids),
                    kind=kind, item=described, taxonomy=taxonomy,
                ),
                video_ids=tuple(video_ids),
                kind=kind,
            )
        )

    if fallback:
        plans.append(
            PlaylistPlan(
                key=fallback_key,
                name=settings.fallback_playlist,
                description=build_description(
                    config.sync.marker, fallback_key, settings.fallback_playlist,
                    len(fallback), kind="fallback", item=None, taxonomy=taxonomy,
                ),
                video_ids=tuple(fallback),
                kind="fallback",
            )
        )

    plans.sort(key=lambda plan: plan.name)
    return plans
