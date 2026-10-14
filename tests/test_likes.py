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


def test_reorder_unlikes_then_likes_from_the_bottom_up():
    calls = []
    steps = []
    report = likes.like_all(
        [_entry("a", liked=True), _entry("b"), _entry("c", liked=True), _entry(None)],
        lambda v: calls.append(("like", v)), unlike=lambda v: calls.append(("unlike", v)),
        reorder=True, sleep=lambda _: None,
        on_progress=lambda i, total, entry, error, step: steps.append((i, total, step)))
    assert calls == [("unlike", "a"), ("unlike", "c"),
                     ("like", "c"), ("like", "b"), ("like", "a")]
    assert (report.unliked, report.liked, report.already, report.unavailable) == (2, 3, 0, 1)
    assert steps[0] == (1, 5, "unliked") and steps[-1] == (5, 5, "liked")


def test_reorder_dry_run_and_limit():
    calls = []
    report = likes.like_all([_entry("a", liked=True), _entry("b"), _entry("c", liked=True)],
                            calls.append, unlike=calls.append, reorder=True, limit=2,
                            dry_run=True)
    assert calls == [] and (report.unliked, report.liked) == (1, 2)


def test_limit_keeps_the_top_of_the_playlist():
    done = []
    likes.like_all([_entry(v) for v in "abcd"], done.append, limit=2, sleep=lambda _: None)
    assert done == ["b", "a"]
