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


def _texts(node: Any) -> list[str]:
    """Les textes affichables (« runs ») d'une réponse de YouTube."""
    found: list[str] = []
    if isinstance(node, dict):
        for run in node.get("runs") or []:
            if isinstance(run, dict) and run.get("text"):
                found.append(run["text"])
        for key, value in node.items():
            if key != "runs":
                found += _texts(value)
    elif isinstance(node, list):
        for value in node:
            found += _texts(value)
    return found


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

    def liked_tracks(self, limit: int | None = None) -> list[Track]:
        """Haut de « Titres likés », du plus récent au plus ancien (tout avec None)."""
        raw = self._api.get_playlist("LM", limit=limit).get("tracks") or []
        tracks = [_to_track(entry, "liked") for entry in raw]
        found = [track for track in tracks if track is not None]
        return found[:limit] if limit else found

    def count_source(self, source: str) -> int | None:
        """Nombre de titres d'une source, sans la lire entièrement quand c'est possible.

        Les playlists — les titres likés en sont une, « LM » — annoncent leur
        `trackCount` dans leur en-tête : un appel suffit. La bibliothèque et les
        mises en ligne, elles, ne l'annoncent nulle part et doivent être
        parcourues.

        Le champ `count` de la liste des playlists, lui, n'est pas exploitable :
        `ytmusicapi` le construit en prenant le premier mot d'un sous-titre, ce
        qui donne « 2 » pour « 2 188 titres », et un mot quelconque selon la
        langue de l'interface.
        """
        if source == "liked":
            return self._api.get_playlist("LM", limit=1).get("trackCount")
        if source.startswith("playlist:"):
            playlist_id = source.split(":", 1)[1]
            return self._api.get_playlist(playlist_id, limit=1).get("trackCount")
        return len(self._scan_source(source))

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
            thumbnail=_thumbnail(data),
        )
        self._playlist_cache[playlist_id] = playlist
        return playlist

    def playlist_entries(self, playlist_id: str) -> list[dict[str, Any]]:
        """Titres d'une playlist : {video_id, label, liked, title, artists}.

        Un titre retiré de YouTube reste affiché, grisé, mais n'a plus
        d'identifiant : il figure avec `video_id` à None, pour être compté.
        """
        data = self._api.get_playlist(playlist_id, limit=None)
        entries = []
        for t in data.get("tracks") or []:
            names = [a.get("name", "") for a in t.get("artists") or [] if a]
            artists = ", ".join(names)
            title = t.get("title") or ""
            entries.append({
                "video_id": t.get("videoId"),
                "label": f"{artists} – {title}" if artists else title,
                "liked": t.get("likeStatus") == "LIKE",
                "title": title,
                "artists": names,
            })
        return entries

    def liked_ids(self, limit: int | None = None) -> list[str]:
        """« Titres likés », du plus récent au plus ancien (les `limit` premiers)."""
        data = self._api.get_playlist("LM", limit=limit)
        ids = [t["videoId"] for t in data.get("tracks") or [] if t.get("videoId")]
        return ids[:limit] if limit else ids

    def like_info(self, video_id: str) -> tuple[bool, set[str]]:
        """Pouce levé sur ce titre, et identifiants de ses versions.

        Un titre existe souvent en version audio et en version clip ;
        « Titres likés » peut afficher l'une quand on a liké l'autre. Les
        identifiants rendus sont ceux des deux versions (le titre compris).
        """
        data = self._api.get_watch_playlist(videoId=video_id, limit=1)
        tracks = data.get("tracks") or []
        if not tracks:
            return False, {video_id}
        first = tracks[0]
        versions = [first] + ([first["counterpart"]] if first.get("counterpart") else [])
        ids = {video_id} | {v.get("videoId") for v in versions if v.get("videoId")}
        own = next((v for v in versions if v.get("videoId") == video_id), first)
        return own.get("likeStatus") == "LIKE", ids

    # ------------------------------------------------------------ écriture

    def _rate(self, video_id: str, status: str) -> None:
        try:
            from ytmusicapi.models.content.enums import LikeStatus

            rating: Any = LikeStatus(status)
        except ImportError:  # anciennes versions : la chaîne suffit
            rating = status
        self._api.rate_song(video_id, rating)

    def like(self, video_id: str) -> None:
        """Pouce levé : le titre rejoint « Titres likés »."""
        self._rate(video_id, "LIKE")

    def unlike(self, video_id: str) -> None:
        """Retire le like (ni pouce levé, ni pouce baissé)."""
        self._rate(video_id, "INDIFFERENT")

    def remove_from_library(self, video_ids: Iterable[str]) -> int:
        """Retire des titres de la bibliothèque. Renvoie combien l'ont été.

        YouTube Music ne retire un titre qu'avec le jeton que porte son entrée
        dans la bibliothèque : on relit donc celle-ci pour les trouver.
        """
        wanted = set(video_ids)
        if not wanted:
            return 0
        tokens = [
            entry["feedbackTokens"]["remove"]
            for entry in self._api.get_library_songs(limit=None)
            if entry.get("videoId") in wanted
            and (entry.get("feedbackTokens") or {}).get("remove")
        ]
        if tokens:
            self._api.edit_song_library_status(tokens)
        return len(tokens)

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

    def add_items(self, playlist_id: str, video_ids: Iterable[str]) -> str | None:
        """Ajoute des titres. Renvoie la raison d'un refus de YouTube, sinon None.

        YouTube refuse tout le lot si l'un des titres y est déjà (doublon) :
        sans ce retour, le refus passerait inaperçu.
        """
        ids = list(video_ids)
        if not ids:
            return None
        response = self._api.add_playlist_items(playlist_id, ids, duplicates=False)
        if not isinstance(response, dict) or "SUCCEEDED" in str(response.get("status", "")):
            return None
        texts = _texts(response.get("actions"))
        return " ".join(texts) or str(response.get("status") or "refus sans explication")

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
