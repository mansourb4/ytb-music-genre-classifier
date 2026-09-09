"""Adaptateur YouTube Music, au-dessus de `ytmusicapi`.

Rappel de conception : YouTube Music ne connaît **que** des playlists — aucun
dossier, aucune imbrication. La hiérarchie genre/style est donc portée par le
nom, et l'appartenance de l'outil à une playlist par un marqueur en description.

`ytmusicapi` s'appuie sur l'API interne de YouTube Music : elle n'est pas
officielle et peut changer. L'import est différé pour que le reste du paquet
(planification, tests) fonctionne sans la dépendance installée.
"""

from __future__ import annotations

from typing import Any, Iterable

from ytmgc.models import RemotePlaylist, Track


#: Sources hors playlists, telles qu'attendues dans la configuration.
SPECIAL_SOURCES = {
    "library": "Bibliothèque (titres ajoutés)",
    "liked": "Titres likés",
    "uploads": "Titres mis en ligne",
}

#: Playlists système, non éditables et sans intérêt comme source.
SYSTEM_PLAYLISTS = {"LM", "SE"}


def validate_sources(sources: list[str]) -> list[str]:
    """Vérifie une sélection de sources avant de lancer quoi que ce soit.

    Sans ce contrôle, une source mal formée n'échouerait qu'au milieu du scan,
    après plusieurs minutes de travail.
    """
    if not sources:
        raise ValueError("Aucune source sélectionnée.")
    for source in sources:
        if source in SPECIAL_SOURCES:
            continue
        if source.startswith("playlist:") and source.split(":", 1)[1]:
            continue
        raise ValueError(f"Source inconnue : {source!r}")
    return list(dict.fromkeys(sources))


def _artists(entry: dict[str, Any]) -> tuple[str, ...]:
    return tuple(a["name"] for a in entry.get("artists") or [] if a.get("name"))


def _thumbnail(entry: dict[str, Any]) -> str | None:
    """URL de la plus grande pochette proposée.

    YouTube Music renvoie plusieurs tailles par ordre croissant ; la plus
    grande reste petite (quelques centaines de pixels) et convient à l'aperçu.
    """
    thumbnails = entry.get("thumbnails") or []
    if not thumbnails:
        return None
    largest = max(thumbnails, key=lambda item: item.get("width") or 0)
    return largest.get("url")


def _album(entry: dict[str, Any]) -> str | None:
    album = entry.get("album")
    if isinstance(album, dict):
        return album.get("name")
    return album if isinstance(album, str) else None


def _to_track(entry: dict[str, Any], source: str) -> Track | None:
    """Convertit une entrée brute ; renvoie None pour les items inexploitables.

    Les titres indisponibles (retirés, bloqués géographiquement) arrivent sans
    `videoId` : les inclure ferait échouer l'ajout à une playlist.
    """
    video_id = entry.get("videoId")
    if not video_id or not entry.get("title"):
        return None
    return Track(
        video_id=video_id,
        title=entry["title"],
        artists=_artists(entry),
        album=_album(entry),
        duration_s=entry.get("duration_seconds"),
        source=source,
        thumbnail=_thumbnail(entry),
    )


class YouTubeMusicClient:
    """Implémente `sync.PlaylistClient` et expose le scan de bibliothèque."""

    def __init__(self, auth_file: str, api: Any | None = None, oauth_client: Any | None = None) -> None:
        if api is None:
            from ytmusicapi import YTMusic  # import différé : dépendance optionnelle

            # Un jeton OAuth ne peut être rafraîchi qu'avec l'identifiant client
            # qui l'a produit : ytmusicapi doit le recevoir à l'ouverture.
            credentials = oauth_client.credentials() if oauth_client is not None else None
            api = YTMusic(auth_file, oauth_credentials=credentials)
        self._api = api
        self._playlist_cache: dict[str, RemotePlaylist] = {}

    # ------------------------------------------------------------- lecture

    def scan(self, sources: Iterable[str]) -> list[Track]:
        """Parcourt les sources demandées et renvoie les titres dédupliqués.

        Un même titre peut apparaître dans la bibliothèque *et* dans les likes ;
        la première source de la liste l'emporte.
        """
        tracks: dict[str, Track] = {}
        for source in sources:
            for track in self._scan_source(source):
                tracks.setdefault(track.video_id, track)
        return list(tracks.values())

    def _scan_source(self, source: str) -> list[Track]:
        if source == "library":
            raw = self._api.get_library_songs(limit=None)
        elif source == "liked":
            raw = self._api.get_liked_songs(limit=10_000).get("tracks", [])
        elif source == "uploads":
            raw = self._api.get_library_upload_songs(limit=None)
        elif source.startswith("playlist:"):
            playlist_id = source.split(":", 1)[1]
            raw = self._api.get_playlist(playlist_id, limit=None).get("tracks", [])
        else:
            raise ValueError(f"Source inconnue : {source!r}")

        tracks = [_to_track(entry, source) for entry in raw]
        return [track for track in tracks if track is not None]

    def list_playlist_summaries(self) -> list[dict[str, Any]]:
        """Playlists de l'utilisateur, sans lire leur contenu.

        Sert à proposer les sources d'analyse : un appel unique, là où
        `list_playlists` en fait un par playlist pour obtenir les descriptions.
        """
        summaries = []
        for entry in self._api.get_library_playlists(limit=None):
            playlist_id = entry.get("playlistId")
            if not playlist_id or playlist_id in SYSTEM_PLAYLISTS:
                continue
            summaries.append(
                {
                    "playlist_id": playlist_id,
                    "title": entry.get("title") or "(sans titre)",
                    "count": entry.get("count"),
                    "thumbnail": _thumbnail(entry),
                }
            )
        return summaries

    def list_playlists(self) -> list[RemotePlaylist]:
        """Playlists de l'utilisateur, description et contenu inclus.

        La liste de la bibliothèque ne porte pas la description : il faut donc
        un appel par playlist pour savoir laquelle est gérée par l'outil. C'est
        le coût d'entrée d'un run ; il reste proportionnel au nombre de
        playlists, pas au nombre de titres.
        """
        playlists = []
        for entry in self._api.get_library_playlists(limit=None):
            playlist_id = entry.get("playlistId")
            # "LM" (Liked Music) et les playlists système ne sont pas éditables.
            if not playlist_id or playlist_id in {"LM", "SE"}:
                continue
            try:
                playlists.append(self.get_playlist(playlist_id))
            except Exception:  # noqa: BLE001 - une playlist illisible ne doit pas tout stopper
                continue
        return playlists

    def get_playlist(self, playlist_id: str) -> RemotePlaylist:
        data = self._api.get_playlist(playlist_id, limit=None)
        tracks = [t for t in data.get("tracks") or [] if t.get("videoId")]
        playlist = RemotePlaylist(
            playlist_id=playlist_id,
            title=data.get("title", ""),
            description=data.get("description") or "",
            video_ids=tuple(t["videoId"] for t in tracks),
            set_video_ids={
                t["videoId"]: t["setVideoId"] for t in tracks if t.get("setVideoId")
            },
        )
        self._playlist_cache[playlist_id] = playlist
        return playlist

    # ------------------------------------------------------------ écriture

    def create_playlist(
        self, title: str, description: str, *, privacy: str, video_ids: Iterable[str]
    ) -> str:
        result = self._api.create_playlist(
            title, description, privacy_status=privacy, video_ids=list(video_ids) or None
        )
        # En cas d'échec, ytmusicapi renvoie un dict de statut plutôt qu'un id.
        if not isinstance(result, str):
            raise RuntimeError(f"Création de playlist refusée par YouTube Music : {result}")
        return result

    def add_items(self, playlist_id: str, video_ids: Iterable[str]) -> None:
        ids = list(video_ids)
        if ids:
            self._api.add_playlist_items(playlist_id, ids, duplicates=False)

    def remove_items(self, playlist_id: str, items: dict[str, str]) -> None:
        if items:
            self._api.remove_playlist_items(
                playlist_id,
                [{"videoId": v, "setVideoId": s} for v, s in items.items()],
            )

    def edit_playlist(
        self, playlist_id: str, *, title: str | None = None, description: str | None = None
    ) -> None:
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["title"] = title
        if description is not None:
            kwargs["description"] = description
        if kwargs:
            self._api.edit_playlist(playlist_id, **kwargs)

    def delete_playlist(self, playlist_id: str) -> None:
        self._api.delete_playlist(playlist_id)
        self._playlist_cache.pop(playlist_id, None)
