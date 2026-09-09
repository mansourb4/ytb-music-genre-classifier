"""Réconciliation entre l'état souhaité (planner) et YouTube Music.

Deux garde-fous structurent ce module :
  * seules les playlists dont la description porte le marqueur sont touchées —
    les playlists créées à la main par l'utilisateur sont invisibles pour l'outil ;
  * le diff est calculé sans aucun appel réseau, ce qui rend le mode « à blanc »
    (dry-run) exact et testable.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Protocol

from ytmgc.config import LEGACY_MARKER, Config
from ytmgc.models import Op, PlaylistPlan, RemotePlaylist, SyncAction

_KEY_RE = re.compile(r"key=([\w\-/]+)")

#: Ancien marqueur, encore présent sur les playlists créées avant le passage à
#: une description lisible. Il reste reconnu pour ne pas les orpheliner.


class PlaylistClient(Protocol):
    """Surface minimale attendue d'un client YouTube Music."""

    def list_playlists(self) -> list[RemotePlaylist]: ...

    def get_playlist(self, playlist_id: str) -> RemotePlaylist: ...

    def create_playlist(
        self, title: str, description: str, *, privacy: str, video_ids: Iterable[str]
    ) -> str: ...

    def add_items(self, playlist_id: str, video_ids: Iterable[str]) -> None: ...

    def remove_items(self, playlist_id: str, items: dict[str, str]) -> None: ...

    def edit_playlist(
        self, playlist_id: str, *, title: str | None = None, description: str | None = None
    ) -> None: ...

    def delete_playlist(self, playlist_id: str) -> None: ...


def is_managed(description: str, marker: str) -> bool:
    """La playlist porte-t-elle la marque de l'outil ?

    C'est le seul critère qui autorise une modification ou une suppression :
    tout ce qui ne la porte pas appartient à l'utilisateur.
    """
    return marker in description or LEGACY_MARKER in description


def extract_key(description: str, marker: str) -> str | None:
    """Clé inscrite dans la description, pour les playlists de l'ancien format."""
    if not is_managed(description, marker):
        return None
    match = _KEY_RE.search(description)
    return match.group(1) if match else None


def managed_playlists(
    playlists: Iterable[RemotePlaylist], marker: str
) -> list[RemotePlaylist]:
    """Toutes les playlists gérées, qu'on sache ou non les rattacher à une clé."""
    return [playlist for playlist in playlists if is_managed(playlist.description, marker)]


def managed_by_key(
    playlists: Iterable[RemotePlaylist],
    marker: str,
    *,
    known: Mapping[str, str] | None = None,
    names: Mapping[str, str] | None = None,
) -> dict[str, RemotePlaylist]:
    """Index des playlists gérées, par clé de genre/style.

    La clé ne figure plus dans la description, devenue purement lisible. Elle
    est retrouvée dans cet ordre :

    1. `known`, l'association identifiant -> clé tenue en base à la création ;
    2. `names`, le nom attendu de chaque playlist prévue — repli utile quand la
       base a été perdue ou que la playlist vient d'une autre machine ;
    3. la description elle-même, pour les playlists de l'ancien format.

    Une playlist gérée mais non rattachable reste hors de l'index : elle ne
    sera donc ni mise à jour ni vidée, seulement laissée telle quelle.
    """
    known = known or {}
    names = names or {}

    index: dict[str, RemotePlaylist] = {}
    for playlist in playlists:
        if not is_managed(playlist.description, marker):
            continue
        key = (
            known.get(playlist.playlist_id)
            or names.get(playlist.title)
            or extract_key(playlist.description, marker)
        )
        if key is not None:
            index[key] = playlist
    return index


def diff(
    plans: Iterable[PlaylistPlan],
    remote: dict[str, RemotePlaylist],
    config: Config,
    untouched: frozenset[str] = frozenset(),
) -> list[SyncAction]:
    """Actions à appliquer pour amener `remote` vers `plans`.

    Les playlists gérées devenues vides ne sont pas supprimées : l'API ne
    permet pas de les restaurer, et un scan partiel ne doit jamais détruire du
    travail. Elles sont seulement vidées quand `sync.prune` est actif.
    """
    actions: list[SyncAction] = []
    planned_keys = set()

    for plan in plans:
        planned_keys.add(plan.key)
        existing = remote.get(plan.key)
        if existing is None:
            actions.append(
                SyncAction(Op.CREATE, plan.key, plan.name, video_ids=plan.video_ids)
            )
            continue

        # Le renommage porte aussi la description : c'est ce qui fait migrer les
        # playlists créées avec l'ancien format, sans quoi elles garderaient
        # indéfiniment leur en-tête technique.
        if existing.title != plan.name or existing.description != plan.description:
            actions.append(
                SyncAction(
                    Op.RENAME,
                    plan.key,
                    existing.title,
                    playlist_id=existing.playlist_id,
                    new_name=plan.name,
                )
            )

        current = set(existing.video_ids)
        missing = tuple(v for v in plan.video_ids if v not in current)
        if missing:
            actions.append(
                SyncAction(
                    Op.ADD, plan.key, plan.name,
                    playlist_id=existing.playlist_id, video_ids=missing,
                )
            )

        if config.sync.prune:
            desired = set(plan.video_ids)
            extra = tuple(v for v in existing.video_ids if v not in desired)
            if extra:
                actions.append(
                    SyncAction(
                        Op.REMOVE, plan.key, plan.name,
                        playlist_id=existing.playlist_id, video_ids=extra,
                    )
                )

    if config.sync.prune:
        for key, playlist in remote.items():
            if key not in planned_keys and key not in untouched and playlist.video_ids:
                actions.append(
                    SyncAction(
                        Op.REMOVE, key, playlist.title,
                        playlist_id=playlist.playlist_id, video_ids=playlist.video_ids,
                    )
                )

    return actions


def _batched(values: tuple[str, ...], size: int) -> Iterable[tuple[str, ...]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def apply(
    actions: Iterable[SyncAction],
    plans: Iterable[PlaylistPlan],
    client: PlaylistClient,
    config: Config,
    *,
    dry_run: bool = True,
    on_created=None,
) -> list[str]:
    """Applique les actions. En dry-run, renvoie seulement leur description.

    `on_created(key, playlist_id, name)` permet au CLI de mémoriser en base les
    playlists créées, sans que ce module ne dépende du dépôt.
    """
    descriptions = {plan.key: plan.description for plan in plans}
    batch = max(1, config.sync.batch_size)
    log: list[str] = []

    for action in actions:
        log.append(("[à blanc] " if dry_run else "") + action.summary())
        if dry_run:
            continue

        if action.op is Op.CREATE:
            head, *rest = list(_batched(action.video_ids, batch)) or [()]
            playlist_id = client.create_playlist(
                action.playlist_name,
                descriptions.get(action.key, ""),
                privacy=config.youtube.playlist_privacy,
                video_ids=head,
            )
            for chunk in rest:
                client.add_items(playlist_id, chunk)
            if on_created is not None:
                on_created(action.key, playlist_id, action.playlist_name)

        elif action.op is Op.ADD:
            assert action.playlist_id is not None
            for chunk in _batched(action.video_ids, batch):
                client.add_items(action.playlist_id, chunk)
            client.edit_playlist(
                action.playlist_id, description=descriptions.get(action.key)
            )

        elif action.op is Op.REMOVE:
            assert action.playlist_id is not None
            playlist = client.get_playlist(action.playlist_id)
            items = {
                video_id: playlist.set_video_ids[video_id]
                for video_id in action.video_ids
                if video_id in playlist.set_video_ids
            }
            if items:
                client.remove_items(action.playlist_id, items)

        elif action.op is Op.RENAME:
            assert action.playlist_id is not None
            client.edit_playlist(
                action.playlist_id,
                title=action.new_name,
                description=descriptions.get(action.key),
            )

    return log


def purge(
    remote: Iterable[RemotePlaylist],
    client: PlaylistClient,
    config: Config,
    *,
    dry_run: bool = True,
    on_deleted=None,
) -> list[str]:
    """Supprime toutes les playlists générées par l'outil.

    C'est l'annulation complète : elle rend le compte à son état d'avant la
    première synchronisation. Comme partout ailleurs, le marqueur délimite la
    portée — une playlist sans marqueur n'est jamais candidate, quelle que soit
    la ressemblance de son nom.

    L'opération est irréversible : YouTube Music ne restaure pas une playlist
    supprimée. Le mode simulation est donc le défaut ici aussi.

    Elle ne s'appuie que sur le marqueur, jamais sur la clé de genre/style :
    une playlist gérée doit rester supprimable même quand plus rien ne permet
    de la rattacher à un couple genre/style.
    """
    log: list[str] = []
    for playlist in sorted(
        managed_playlists(remote, config.sync.marker), key=lambda item: item.title
    ):
        log.append(
            ("[à blanc] " if dry_run else "")
            + f"supprimer « {playlist.title} » ({len(playlist.video_ids)} titres)"
        )
        if dry_run:
            continue
        client.delete_playlist(playlist.playlist_id)
        if on_deleted is not None:
            on_deleted(playlist.playlist_id)
    return log
