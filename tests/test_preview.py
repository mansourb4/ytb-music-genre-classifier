from conftest import make_candidate
from fakes import FakeDiscogs, FakePlaylistClient

from ytmgc.classifier import classify_tracks
from ytmgc.models import Classification, MatchStatus, RemotePlaylist, Track
from ytmgc.preview import build_preview
from ytmgc.sorting import apply_sort_mode

CATALOGUE = {
    "Nirvana": [make_candidate(1, "Nevermind", "Nirvana", ("Rock",), ("Grunge",))],
    "Aphex Twin": [make_candidate(2, "SAW", "Aphex Twin", ("Electronic",), ("Ambient", "IDM"))],
}
LIBRARY = [
    Track("g1", "Come As You Are", ("Nirvana",), "Nevermind"),
    Track("g2", "Lithium", ("Nirvana",), "Nevermind"),
    Track("e1", "Xtal", ("Aphex Twin",), "SAW"),
    Track("e2", "Ageispolis", ("Aphex Twin",), "SAW"),
]


def seeded(repository, config):
    repository.upsert_tracks(LIBRARY)
    classify_tracks(LIBRARY, repository, FakeDiscogs(CATALOGUE), config)
    return repository


def test_preview_on_an_empty_account_reports_only_creations(repository, config):
    seeded(repository, config)
    summary, plans, actions = build_preview(repository, config, sort_mode="detaille")

    assert summary.created == len(summary.playlists)
    assert summary.updated == 0
    assert {p.change for p in summary.playlists} == {"création"}
    assert len(plans) == len(summary.playlists)


def test_preview_counts_reflect_the_library_state(repository, config):
    seeded(repository, config)
    repository.upsert_tracks([Track("x1", "Inconnu", ("Personne",))])
    summary, _, _ = build_preview(repository, config, sort_mode="detaille")

    assert summary.total_tracks == 5
    assert summary.classified == 4
    assert summary.unclassified == 1


def test_assignments_exceed_tracks_in_multi_style_mode(repository, config):
    """En mode "all", un titre à deux styles est compté deux fois : l'aperçu
    doit le montrer plutôt que de laisser croire à une incohérence."""
    seeded(repository, config)
    summary, _, _ = build_preview(repository, config, sort_mode="detaille")
    assert summary.assignments > summary.classified


def test_preview_shows_sample_tracks(repository, config):
    seeded(repository, config)
    summary, _, _ = build_preview(repository, config, sort_mode="detaille")
    grunge = next(p for p in summary.playlists if p.name.endswith("Grunge"))
    assert "Nirvana – Come As You Are" in grunge.sample


def test_preview_against_an_existing_state_distinguishes_changes(repository, config):
    seeded(repository, config)
    remote = [
        RemotePlaylist(
            "PL1", "Rock — Grunge", "[ytmgc] key=rock/grunge", ("g1", "obsolete"),
            {"g1": "s1", "obsolete": "s2"},
        )
    ]
    summary, _, _ = build_preview(repository, config, sort_mode="detaille", remote=remote)

    grunge = next(p for p in summary.playlists if p.key == "rock/grunge")
    assert grunge.change == "mise à jour"
    assert (grunge.added, grunge.removed) == (1, 1)
    assert summary.updated == 1


def test_unchanged_playlist_is_reported_as_such(repository, config):
    seeded(repository, config)
    _, plans, _ = build_preview(repository, config, sort_mode="detaille")
    grunge_plan = next(p for p in plans if p.key == "rock/grunge")
    remote = [RemotePlaylist("PL1", grunge_plan.name, grunge_plan.description, grunge_plan.video_ids)]

    summary, _, _ = build_preview(repository, config, sort_mode="detaille", remote=remote)
    assert next(p for p in summary.playlists if p.key == "rock/grunge").change == "inchangée"


def test_obsolete_managed_playlists_are_listed(repository, config):
    seeded(repository, config)
    remote = [RemotePlaylist("PL9", "Jazz — Bebop", "[ytmgc] key=jazz/bebop", ("z",))]
    summary, _, _ = build_preview(repository, config, sort_mode="detaille", remote=remote)
    assert summary.obsolete == ["Jazz — Bebop"]


def test_sort_mode_changes_the_preview(repository, config):
    # Les modes portent leurs propres seuils (4 titres par style) : il faut une
    # bibliothèque assez fournie pour que les playlists de style existent.
    library = LIBRARY + [
        Track(f"g{i}", f"Titre {i}", ("Nirvana",), "Nevermind") for i in range(3, 7)
    ] + [Track(f"e{i}", f"Piste {i}", ("Aphex Twin",), "SAW") for i in range(3, 7)]
    repository.upsert_tracks(library)
    classify_tracks(library, repository, FakeDiscogs(CATALOGUE), config)

    detailed, _, _ = build_preview(
        repository, apply_sort_mode(config, "detaille"), sort_mode="detaille"
    )
    by_genre, _, _ = build_preview(
        repository, apply_sort_mode(config, "genre"), sort_mode="genre"
    )
    assert len(by_genre.playlists) < len(detailed.playlists)
    assert all(p.kind != "style" for p in by_genre.playlists)


def test_preview_is_json_serialisable(repository, config):
    import json

    seeded(repository, config)
    summary, _, _ = build_preview(repository, config, sort_mode="detaille")
    assert json.loads(json.dumps(summary.to_dict()))["sort_mode"] == "detaille"


def test_preview_of_an_unclassified_library_is_empty_but_valid(repository, config):
    repository.upsert_tracks(LIBRARY)
    summary, plans, actions = build_preview(repository, config, sort_mode="detaille")
    assert summary.playlists == [] and plans == [] and actions == []
    assert summary.unclassified == 4
