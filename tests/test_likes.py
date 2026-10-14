from ytmgc import likes


def _entry(video_id, liked=False):
    return {"video_id": video_id, "label": f"Artiste – {video_id}", "liked": liked}


def test_find_playlists_prefers_the_exact_name():
    summaries = [{"title": "perso - 🎧"}, {"title": "perso - 🎧 (vieux)"}, {"title": "Jazz"}]
    assert likes.find_playlists(summaries, "perso - 🎧") == [summaries[0]]
    assert likes.find_playlists(summaries, "PERSO") == summaries[:2]
    assert likes.find_playlists(summaries, "rock") == []


def test_already_liked_and_repeated_titles_are_skipped():
    done = []
    report = likes.like_all([_entry("a", liked=True), _entry("b"), _entry("b"), _entry("c")],
                            done.append, sleep=lambda _: None)
    # Du bas vers le haut : le premier titre devient le like le plus récent.
    assert done == ["c", "b"]
    assert (report.liked, report.already, report.failed) == (2, 1, [])


def test_a_dry_run_likes_nothing():
    done = []
    report = likes.like_all([_entry("a"), _entry("b", liked=True)], done.append, dry_run=True)
    assert done == [] and (report.liked, report.already) == (1, 1)


def test_failures_are_noted_and_too_many_stop():
    def refuse(video_id):
        raise RuntimeError("Server returned HTTP 429: Too Many Requests.\nDétail")

    report = likes.like_all([_entry(v) for v in "abcdef"], refuse, sleep=lambda _: None)
    assert report.stopped and len(report.failed) == likes.MAX_CONSECUTIVE_FAILURES
    assert report.failed[0] == ("Artiste – f", "Server returned HTTP 429: Too Many Requests.")


def test_a_pause_separates_likes():
    waits = []
    likes.like_all([_entry(v) for v in "abc"], lambda v: None, pause=1.5, sleep=waits.append)
    assert waits == [1.5, 1.5]


def test_titles_removed_from_youtube_are_counted_apart():
    done = []
    report = likes.like_all([_entry("a"), _entry(None)], done.append, sleep=lambda _: None)
    assert done == ["a"] and report.unavailable == 1


def test_limit_keeps_the_top_of_the_playlist():
    done = []
    likes.like_all([_entry(v) for v in "abcd"], done.append, limit=2, sleep=lambda _: None)
    assert done == ["b", "a"]


def test_check_counts_missing_and_misplaced_titles():
    playlist = [_entry(v) for v in "abcde"] + [_entry(None), _entry("a")]
    liked = [_entry(v) for v in ["x", "a", "c", "b", "e"]]
    found = likes.check(playlist, liked)
    assert (found.wanted, found.liked_total, found.first_rank) == (5, 5, 1)
    assert [e["video_id"] for e in found.missing] == ["d"]
    assert found.out_of_order == 1
    assert found.missing_by_slice == [(5, 1)]


class FakeYouTube:
    """« Titres likés » du plus récent au plus ancien, qui perd des demandes.

    Un like sur un titre déjà liké ne le déplace pas ; `drop` : numéros des
    demandes (likes et retraits confondus) que YouTube « accepte » sans rien
    faire ; `hidden` : titres likés que « Titres likés » n'affiche jamais.
    """

    def __init__(self, liked=(), drop=(), hidden=(), stuck=()):
        self.stuck = set(stuck)
        self.order = list(liked)
        self.thumbs = set(liked)
        self.drop = set(drop)
        self.hidden = set(hidden)
        self.calls = 0

    def _dropped(self):
        self.calls += 1
        return self.calls in self.drop

    def like(self, video_id):
        if self._dropped() or video_id in self.thumbs:
            return
        self.thumbs.add(video_id)
        if video_id not in self.hidden:
            self.order.insert(0, video_id)

    def unlike(self, video_id):
        if self._dropped() or video_id in self.stuck:
            return
        self.thumbs.discard(video_id)
        if video_id in self.order:
            self.order.remove(video_id)

    def liked_ids(self, limit=None):
        return self.order[:limit] if limit else list(self.order)

    def is_liked(self, video_id):
        return video_id in self.thumbs


def _reorder(youtube, entries, **kwargs):
    lines = []
    report = likes.reorder(entries, like=youtube.like, unlike=youtube.unlike,
                           liked_ids=youtube.liked_ids, is_liked=youtube.is_liked,
                           pause=0, sleep=lambda _: None, on_line=lines.append, **kwargs)
    return report, lines


def test_reorder_puts_the_playlist_order_on_top_despite_dropped_requests():
    playlist = [_entry(v, liked=v in "bdf") for v in "abcdefgh"] + [_entry(None)]
    # Likés à l'envers, plus deux titres hors playlist, plus anciens.
    youtube = FakeYouTube(liked=["b", "d", "f", "x", "y"], drop={2, 5, 9, 14, 15})
    report, lines = _reorder(youtube, playlist, batch=3)
    assert youtube.order == list("abcdefgh") + ["x", "y"]
    assert report.stopped is None and report.retries > 0
    assert (report.liked, report.unavailable) == (8, 1)


def test_reorder_sets_titles_youtube_music_never_shows_aside():
    youtube = FakeYouTube(hidden={"c"})
    report, _ = _reorder(youtube, [_entry(v) for v in "abcd"], batch=2)
    assert youtube.order == ["a", "b", "d"]
    assert [e["video_id"] for e in report.hidden] == ["c"]
    assert report.retries == 0 and report.stopped is None


def test_reorder_resumes_after_the_titles_already_in_place():
    # Interrompu : d puis c déjà likés dans l'ordre, a et b pas encore.
    youtube = FakeYouTube(liked=["c", "d", "a", "z"])
    report, lines = _reorder(youtube, [_entry(v, liked=v in "acd") for v in "abcd"], batch=10)
    assert youtube.order == ["a", "b", "c", "d", "z"]
    assert report.kept == 2 and report.liked == 2
    assert "2 titre(s) déjà en place" in lines[0]


def test_reorder_stops_when_youtube_keeps_dropping():
    youtube = FakeYouTube(drop=range(1, 1000))
    report, _ = _reorder(youtube, [_entry(v) for v in "ab"], waits=(1, 1))
    assert report.stopped and report.liked == 0


def test_reorder_limit_takes_the_top_of_the_playlist():
    youtube = FakeYouTube(liked=["d", "c"])
    _reorder(youtube, [_entry(v, liked=v in "cd") for v in "abcd"], limit=2)
    assert youtube.order == ["a", "b", "d", "c"]


def test_likes_that_never_go_away_are_left_in_place():
    youtube = FakeYouTube(liked=["c", "b", "x"], stuck={"b"})
    report, lines = _reorder(youtube, [_entry(v, liked=v in "bc") for v in "abc"],
                             waits=(1, 1, 1))
    assert youtube.order == ["a", "c", "b", "x"]
    assert [(e["video_id"], thumb) for e, thumb in report.stuck] == [("b", True)]
    assert report.retries == 1 and report.stopped is None
    assert any("impossibles à retirer" in line for line in lines)
