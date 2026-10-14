import pytest

from ytmgc.sources.ytmusic import YouTubeMusicClient


class FakeApi:
    """Double de `ytmusicapi.YTMusic`, avec les formes de réponse réelles."""

    def __init__(self, library=None, liked=None, playlists=None, details=None):
        self.library = library or []
        self.liked = liked or []
        self.playlists = playlists or []
        self.details = details or {}
        self.calls = []

    def get_library_songs(self, limit=None):
        return self.library

    def get_liked_songs(self, limit=None):
        return {"tracks": self.liked}

    def get_library_playlists(self, limit=None):
        return self.playlists

    def get_playlist(self, playlist_id, limit=None):
        if playlist_id not in self.details:
            raise KeyError(playlist_id)
        return self.details[playlist_id]

    def create_playlist(self, title, description, privacy_status=None, video_ids=None):
        self.calls.append(("create", title, privacy_status, tuple(video_ids or ())))
        return "PLnew"

    def add_playlist_items(self, playlist_id, video_ids, duplicates=False):
        self.calls.append(("add", playlist_id, tuple(video_ids)))

    def remove_playlist_items(self, playlist_id, videos):
        self.calls.append(("remove", playlist_id, tuple(v["setVideoId"] for v in videos)))

    def edit_playlist(self, playlist_id, **kwargs):
        self.calls.append(("edit", playlist_id, kwargs))


def song(video_id, title="T", artist="A", album="Al"):
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist, "id": "UC1"}],
        "album": {"name": album, "id": "MPRE1"},
        "duration_seconds": 210,
    }


def test_scan_reads_several_sources_and_dedupes():
    api = FakeApi(library=[song("v1"), song("v2")], liked=[song("v2"), song("v3")])
    tracks = YouTubeMusicClient("auth", api=api).scan(["library", "liked"])
    assert [t.video_id for t in tracks] == ["v1", "v2", "v3"]
    # La première source l'emporte : v2 vient de la bibliothèque.
    assert next(t for t in tracks if t.video_id == "v2").source == "library"


def test_unavailable_tracks_are_skipped():
    """Sans videoId, un titre ferait échouer l'ajout à une playlist."""
    api = FakeApi(library=[song("v1"), {"title": "Retiré", "artists": []}, {"videoId": "v9"}])
    assert [t.video_id for t in YouTubeMusicClient("a", api=api).scan(["library"])] == ["v1"]


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError, match="Source inconnue"):
        YouTubeMusicClient("a", api=FakeApi()).scan(["dossiers"])


def test_playlist_source_is_supported():
    api = FakeApi(details={"PL1": {"title": "X", "tracks": [song("v1")]}})
    tracks = YouTubeMusicClient("a", api=api).scan(["playlist:PL1"])
    assert tracks[0].source == "playlist:PL1"


def test_get_playlist_exposes_set_video_ids_needed_for_removal():
    api = FakeApi(details={"PL1": {
        "title": "Rock — Grunge",
        "description": "[ytmgc] key=rock/grunge",
        "tracks": [dict(song("v1"), setVideoId="s1"), song("v2")],
    }})
    playlist = YouTubeMusicClient("a", api=api).get_playlist("PL1")
    assert playlist.video_ids == ("v1", "v2")
    assert playlist.set_video_ids == {"v1": "s1"}


def test_system_playlists_are_never_listed():
    """« Liked Music » n'est pas éditable : la lister ferait échouer le sync."""
    api = FakeApi(
        playlists=[{"playlistId": "LM"}, {"playlistId": "PL1"}, {"title": "sans id"}],
        details={"PL1": {"title": "X", "description": "", "tracks": []}},
    )
    assert [p.playlist_id for p in YouTubeMusicClient("a", api=api).list_playlists()] == ["PL1"]


def test_an_unreadable_playlist_does_not_abort_the_listing():
    api = FakeApi(
        playlists=[{"playlistId": "PLbroken"}, {"playlistId": "PL1"}],
        details={"PL1": {"title": "X", "description": "", "tracks": []}},
    )
    assert len(YouTubeMusicClient("a", api=api).list_playlists()) == 1


def test_create_playlist_reports_api_refusals():
    class Refusing(FakeApi):
        def create_playlist(self, *args, **kwargs):
            return {"status": "STATUS_FAILED"}

    with pytest.raises(RuntimeError, match="refusée"):
        YouTubeMusicClient("a", api=Refusing()).create_playlist(
            "T", "d", privacy="PRIVATE", video_ids=["v1"]
        )


def test_write_operations_are_no_ops_when_empty():
    api = FakeApi()
    client = YouTubeMusicClient("a", api=api)
    client.add_items("PL1", [])
    client.remove_items("PL1", {})
    client.edit_playlist("PL1")
    assert api.calls == []


def test_playlist_entries_carry_the_like_status():
    api = FakeApi(details={"PL1": {"tracks": [
        {"videoId": "a", "title": "Rain", "artists": [{"name": "Moderat"}], "likeStatus": "LIKE"},
        {"videoId": "b", "title": "Solo", "artists": [], "likeStatus": "INDIFFERENT"},
        {"videoId": None, "title": "Indisponible"},
    ]}})
    entries = YouTubeMusicClient("unused", api=api).playlist_entries("PL1")
    assert [(e["video_id"], e["label"], e["liked"]) for e in entries] == [
        ("a", "Moderat – Rain", True),
        ("b", "Solo", False),
        (None, "Indisponible", False),
    ]
    assert (entries[0]["title"], entries[0]["artists"]) == ("Rain", ["Moderat"])


def test_like_and_unlike_rate_the_song():
    rated = []

    class Api(FakeApi):
        def rate_song(self, video_id, rating):
            rated.append((video_id, str(getattr(rating, "value", rating))))

    client = YouTubeMusicClient("unused", api=Api())
    client.like("a")
    client.unlike("b")
    assert rated == [("a", "LIKE"), ("b", "INDIFFERENT")]


def test_remove_from_library_uses_the_entry_tokens():
    sent = []

    class Api(FakeApi):
        def edit_song_library_status(self, tokens):
            sent.append(list(tokens))

    api = Api(library=[
        {"videoId": "a", "title": "A", "feedbackTokens": {"add": "+a", "remove": "-a"}},
        {"videoId": "b", "title": "B", "feedbackTokens": {"add": "+b", "remove": "-b"}},
        {"videoId": "c", "title": "C"},
    ])
    client = YouTubeMusicClient("unused", api=api)
    assert client.remove_from_library(["a", "c", "z"]) == 1
    assert sent == [["-a"]]
    assert client.remove_from_library([]) == 0


def test_liked_ids_and_like_info():
    class Api(FakeApi):
        def get_watch_playlist(self, videoId=None, limit=25):
            if videoId == "gone":
                return {"tracks": []}
            if videoId == "clip":  # la version audio passe devant, le clip en contrepartie
                return {"tracks": [{"videoId": "audio", "likeStatus": "INDIFFERENT",
                                    "counterpart": {"videoId": "clip", "likeStatus": "LIKE"}}]}
            status = "LIKE" if videoId == "a" else "INDIFFERENT"
            return {"tracks": [{"videoId": videoId, "likeStatus": status},
                               {"videoId": "z", "likeStatus": "LIKE"}]}

    api = Api(details={"LM": {"tracks": [{"videoId": v} for v in "abc"] + [{"videoId": None}]}})
    client = YouTubeMusicClient("unused", api=api)
    assert client.liked_ids() == ["a", "b", "c"]
    assert client.liked_ids(2) == ["a", "b"]
    assert client.like_info("a") == (True, {"a"})
    assert client.like_info("b") == (False, {"b"})
    assert client.like_info("clip") == (True, {"clip", "audio"})
    assert client.like_info("gone") == (False, {"gone"})


def test_add_items_reports_why_youtube_refused():
    class Api(FakeApi):
        def add_playlist_items(self, playlist_id, video_ids, duplicates=False):
            if "dup" in video_ids:
                return {"status": "STATUS_FAILED", "actions": [{"addToToastAction": {"item": {
                    "notificationActionRenderer": {"responseText": {"runs": [
                        {"text": "Ce titre figure déjà dans la playlist"}]}}}}}]}
            return {"status": "STATUS_SUCCEEDED", "playlistEditResults": []}

    client = YouTubeMusicClient("unused", api=Api())
    assert client.add_items("PL1", ["a"]) is None
    assert client.add_items("PL1", ["a", "dup"]) == "Ce titre figure déjà dans la playlist"
    assert client.add_items("PL1", []) is None
