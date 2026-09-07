"""Réconciliation entre l'état souhaité (planner) et YouTube Music.

Deux garde-fous structurent ce module :
  * seules les playlists dont la description porte le marqueur sont touchées —
    les playlists créées à la main par l'utilisateur sont invisibles pour l'outil ;
  * le diff est calculé sans aucun appel réseau, ce qui rend le mode « à blanc »
    (dry-run) exact et testable.
"""

from __future__ import annotations

import re
from typing import Iterable, Protocol

from ytmgc.config import Config
from ytmgc.models import Op, PlaylistPlan, RemotePlaylist, SyncAction

_KEY_RE = re.compile(r"key=([\w\-/]+)")


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


def extract_key(description: str, marker: str) -> str | None:
    """Clé de playlist lue dans la description, si la playlist est gérée par l'outil."""
    if marker not in description:
        return None
    match = _KEY_RE.search(description)
    return match.group(1) if match else None


def managed_by_key(playlists: Iterable[RemotePlaylist], marker: str) -> dict[str, RemotePlaylist]:
    """Index des playlists gérées, par clé. Les autres sont ignorées."""
    index: dict[str, RemotePlaylist] = {}
    for playlist in playlists:
        key = extract_key(playlist.description, marker)
        if key is not None:
            index[key] = playlist
    return index


def diff(
    plans: Iterable[PlaylistPlan],
    remote: dict[str, RemotePlaylist],
    config: Config,
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

        if existing.title != plan.name:
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
            if key not in planned_keys and playlist.video_ids:
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
    """
    log: list[str] = []
    for key, playlist in sorted(managed_by_key(remote, config.sync.marker).items()):
        log.append(
            ("[à blanc] " if dry_run else "")
            + f"supprimer « {playlist.title} » ({len(playlist.video_ids)} titres)"
        )
        if dry_run:
            continue
        client.delete_playlist(playlist.playlist_id)
        if on_deleted is not None:
            on_deleted(key)
    return log
