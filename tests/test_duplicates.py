"""Ce que l'outil considère comme un même morceau, montré à l'utilisateur."""

from ytmgc.duplicates import find_duplicates
from ytmgc.models import Track

LIBRARY = [
    Track("a1", "Smells Like Teen Spirit", ("Nirvana",), "Nevermind"),
    Track("a2", "Nirvana - Smells Like Teen Spirit (Official Music Video)", ("NirvanaVEVO",)),
    Track("a3", "Smells Like Teen Spirit", ("Nirvana - Topic",), "Nevermind"),
    Track("a4", "Smells Like Teen Spirit (Live at Reading)", ("Nirvana",), "Live at Reading"),
    Track("b1", "Get Lucky", ("Daft Punk", "Pharrell Williams"), "RAM"),
    Track("b2", "Get Lucky (Radio Edit)", ("Daft Punk", "Pharrell Williams", "Nile Rodgers")),
    Track("c1", "Creep", ("Radiohead",), "Pablo Honey"),
    Track("c2", "Creep (Acoustic)", ("Radiohead",)),
    Track("c3", "Creeping Death", ("Radiohead",)),
]


def test_the_report_counts_tracks_and_distinct_songs():
    report = find_duplicates(LIBRARY)

    assert report.tracks == 9
    assert report.distinct == 6
    assert report.merged == 3


def test_groups_list_every_publication_largest_first():
    groups = find_duplicates(LIBRARY).groups

    assert [len(group.tracks) for group in groups] == [3, 2]
    assert {track.video_id for track in groups[0].tracks} == {"a1", "a2", "a3"}


def test_close_versions_are_listed_but_kept_apart():
    pairs = {(a.video_id, b.video_id) for a, b in find_duplicates(LIBRARY).versions}

    assert ("a1", "a4") in pairs
    assert ("c1", "c2") in pairs


def test_a_longer_word_is_not_mistaken_for_a_version():
    """« Creeping Death » n'est pas une version de « Creep »."""
    pairs = {(a.video_id, b.video_id) for a, b in find_duplicates(LIBRARY).versions}
    assert ("c1", "c3") not in pairs


def test_an_empty_library_reports_nothing():
    report = find_duplicates([])
    assert (report.tracks, report.distinct, report.groups, report.versions) == (0, 0, [], [])
