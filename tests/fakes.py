"""Doubles en mémoire des API externes, pour tester sans réseau."""

from __future__ import annotations

from typing import Iterable

from ytmgc.models import ReleaseCandidate, RemotePlaylist, Track


class FakePlaylistClient:
    """Implémentation en mémoire de `sync.PlaylistClient`."""

    def __init__(self, playlists: list[RemotePlaylist] | None = None) -> None:
        self._playlists = {p.playlist_id: p for p in playlists or []}
        self._next_id = len(self._playlists) + 1
        self.calls: list[str] = []

    def list_playlists(self) -> list[RemotePlaylist]:
        self.calls.append("list")
        return list(self._playlists.values())

    def get_playlist(self, playlist_id: str) -> RemotePlaylist:
        return self._playlists[playlist_id]

    def create_playlist(
        self, title: str, description: str, *, privacy: str, video_ids: Iterable[str]
    ) -> str:
        playlist_id = f"PL{self._next_id}"
        self._next_id += 1
        ids = tuple(video_ids)
        self._playlists[playlist_id] = RemotePlaylist(
            playlist_id=playlist_id,
            title=title,
            description=description,
            video_ids=ids,
            set_video_ids={v: f"set-{v}" for v in ids},
        )
        self.calls.append(f"create:{title}")
        return playlist_id

    def add_items(self, playlist_id: str, video_ids: Iterable[str]) -> None:
        current = self._playlists[playlist_id]
        added = tuple(v for v in video_ids if v not in current.video_ids)
        self._playlists[playlist_id] = RemotePlaylist(
            playlist_id=playlist_id,
            title=current.title,
            description=current.description,
            video_ids=current.video_ids + added,
            set_video_ids={**current.set_video_ids, **{v: f"set-{v}" for v in added}},
        )
        self.calls.append(f"add:{playlist_id}:{len(added)}")

    def remove_items(self, playlist_id: str, items: dict[str, str]) -> None:
        current = self._playlists[playlist_id]
        remaining = tuple(v for v in current.video_ids if v not in items)
        self._playlists[playlist_id] = RemotePlaylist(
            playlist_id=playlist_id,
            title=current.title,
            description=current.description,
            video_ids=remaining,
            set_video_ids={v: s for v, s in current.set_video_ids.items() if v not in items},
        )
        self.calls.append(f"remove:{playlist_id}:{len(items)}")

    def edit_playlist(
        self, playlist_id: str, *, title: str | None = None, description: str | None = None
    ) -> None:
        current = self._playlists[playlist_id]
        self._playlists[playlist_id] = RemotePlaylist(
            playlist_id=playlist_id,
            title=title if title is not None else current.title,
            description=description if description is not None else current.description,
            video_ids=current.video_ids,
            set_video_ids=current.set_video_ids,
        )
        self.calls.append(f"edit:{playlist_id}")

    def delete_playlist(self, playlist_id: str) -> None:
        del self._playlists[playlist_id]
        self.calls.append(f"delete:{playlist_id}")


class FakeDiscogs:
    """Source de candidats pilotée par une table artiste -> releases."""

    def __init__(self, by_artist: dict[str, list[ReleaseCandidate]]) -> None:
        self._by_artist = {key.lower(): value for key, value in by_artist.items()}
        self.searches: list[str] = []

    def search(self, track: Track, *, limit: int = 10) -> list[ReleaseCandidate]:
        self.searches.append(track.label())
        return self._by_artist.get(track.artist.lower(), [])[:limit]
