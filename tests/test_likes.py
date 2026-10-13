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
    assert done == ["b", "c"]
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
    assert report.failed[0] == ("Artiste – a", "Server returned HTTP 429: Too Many Requests.")


def test_a_pause_separates_likes():
    waits = []
    likes.like_all([_entry(v) for v in "abc"], lambda v: None, pause=1.5, sleep=waits.append)
    assert waits == [1.5, 1.5]
