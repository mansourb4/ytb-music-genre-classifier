from conftest import make_candidate, make_track

from ytmgc.matching import best_match, score_candidate


def test_exact_artist_and_album_scores_high():
    track = make_track("v1", "Come As You Are (Official Video)", "Nirvana", album="Nevermind")
    scored = score_candidate(track, make_candidate(1, "Nevermind", "Nirvana (2)"))
    assert scored.score > 0.72
    assert scored.breakdown["artist"] == 1.0
    assert scored.breakdown["release"] == 1.0


def test_wrong_artist_is_rejected():
    track = make_track("v1", "Come As You Are", "Nirvana", album="Nevermind")
    scored = score_candidate(track, make_candidate(2, "Greatest Hits", "Queen"))
    assert scored.score < 0.55


def test_missing_album_is_neutral_not_penalising():
    """Les titres likés n'ont pas toujours d'album : ils restent classables.

    La recherche s'est alors faite par piste : un titre de release différent
    est normal et ne doit pas disqualifier le candidat.
    """
    track = make_track("v2", "Lithium", "Nirvana")
    candidate = make_candidate(3, "Nevermind", "Nirvana")
    assert score_candidate(track, candidate).breakdown["release"] == 0.5
    assert score_candidate(track, candidate).score > 0.72


def test_confidence_floor_ignores_coincidental_similarity():
    """Régression : "Moon Safari" et "Talkie Walkie" se ressemblent à ~0.25 pour
    SequenceMatcher. Ce bruit ne doit rapporter aucun point, sinon un mauvais
    album passe au-dessus du seuil d'acceptation."""
    track = make_track("v1", "La femme d'argent", "Air", album="Moon Safari")
    scored = score_candidate(track, make_candidate(5, "Talkie Walkie", "Air", ("Electronic",), ("Downtempo",)))
    assert scored.breakdown["release"] == 0.0
    assert 0.55 <= scored.score < 0.72  # zone de vérification manuelle


def test_close_artist_spelling_still_matches():
    track = make_track("v1", "Glory Box", "Portishead", album="Dummy")
    scored = score_candidate(track, make_candidate(6, "Dummy", "Portis Head", ("Electronic",), ("Trip Hop",)))
    assert scored.score > 0.72


def test_best_match_prefers_candidate_with_styles():
    track = make_track("v1", "Nevermind", "Nirvana", album="Nevermind")
    bare = make_candidate(1, "Nevermind", "Nirvana", genres=(), styles=())
    rich = make_candidate(2, "Nevermind", "Nirvana", ("Rock",), ("Grunge",))
    assert best_match(track, [bare, rich]).candidate.discogs_id == 2


def test_best_match_returns_none_without_usable_candidates():
    track = make_track("v1", "T", "A")
    assert best_match(track, []) is None
    assert best_match(track, [make_candidate(1, "T", "A", genres=(), styles=())]) is None


def test_collaboration_matches_on_any_credited_artist():
    """Discogs ne crédite souvent qu'un artiste là où YouTube en liste plusieurs."""
    track = make_track("v1", "Get Lucky", "Daft Punk", album="Random Access Memories")
    candidate = make_candidate(
        4, "Random Access Memories", "Daft Punk", ("Electronic",), ("Disco", "Synth-Pop")
    )
    assert score_candidate(track, candidate).breakdown["artist"] == 1.0
