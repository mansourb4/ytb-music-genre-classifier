"""Tri « Par famille » : un plan de playlists écrit à l'avance.

Ce qui est vérifié ici, c'est ce qui rendait le tri automatique imprévisible :
l'ordre décide des cas limites, un morceau n'est jamais versé dans un
fourre-tout sans qu'on le voie, et une faute dans le fichier se signale au
lieu de faire taire une règle.
"""

import pytest
from conftest import make_candidate
from fakes import FakeDiscogs

from ytmgc import cli, verdicts
from ytmgc.classifier import classify_tracks
from ytmgc.models import Classification, MatchStatus, Track
from ytmgc.playlist_plan import (
    PlanError,
    assign,
    load_plan,
    parse_plan,
    plan_by_rules,
)
from ytmgc.preview import build_preview
from ytmgc.sorting import apply_sort_mode
from ytmgc.taxonomy import load_taxonomy
from ytmgc.verdicts import Verdict, VerdictBook

TAXONOMY = load_taxonomy()

PLAN = """
[taille]
min = 2
max = 3

[[playlist]]
nom = "Jazz · Jazz-funk & groove"
description = "Jazz-funk, et la fusion quand elle groove."
styles = ["Jazz-Funk"]

[[playlist]]
nom = "Jazz · Jazz-funk & groove"
styles = ["Fusion"]
ambiances = ["Groovy"]

[[playlist]]
nom = "Jazz · Fusion"
styles = ["Fusion"]

[[playlist]]
nom = "Brésil · Bossa nova"
styles = ["Bossa Nova"]

[[playlist]]
nom = "Jazz · Divers"
genres = ["Jazz"]

[[playlist]]
nom = "Soul · Soul"
genres = ["Funk & Soul"]

[[playlist]]
nom = "Rap · Hip hop"
genres = ["Hip Hop"]
styles = ["Hip Hop"]
"""


def judged(video_id, genre, style, mood=None):
    return Classification(video_id, MatchStatus.UNMATCHED, genres=(genre,),
                          styles=(style,) if style else (), mood=mood, judged=True)


@pytest.fixture
def plan():
    return parse_plan(PLAN, TAXONOMY)


# ------------------------------------------------------------- affectation


def test_the_first_matching_rule_wins(plan):
    """Une règle étroite placée avant une large prend ce qui la concerne."""
    groovy = assign(judged("a", "Jazz", "Fusion", "Groovy"), plan, TAXONOMY)
    cerebral = assign(judged("b", "Jazz", "Fusion", "Cérébral"), plan, TAXONOMY)

    assert groovy.name == "Jazz · Jazz-funk & groove"
    assert cerebral.name == "Jazz · Fusion"


def test_a_cross_genre_style_gathers_whatever_genre_claude_gave():
    """La bossa nova rangée sous Jazz par un verdict et sous Latin par un autre
    finit dans la même playlist : c'était le principal éparpillement."""
    plan = parse_plan(PLAN, TAXONOMY)
    assert assign(judged("a", "Jazz", "Bossa Nova"), plan, TAXONOMY).name == "Brésil · Bossa nova"
    assert assign(judged("b", "Latin", "Bossa Nova"), plan, TAXONOMY).name == "Brésil · Bossa nova"


def test_a_genre_rule_catches_the_rest_of_its_family(plan):
    assert assign(judged("a", "Jazz", "Ballad", "Calme"), plan, TAXONOMY).name == "Jazz · Divers"


def test_a_discogs_genre_is_read_under_its_displayed_name(plan):
    """Discogs écrit « Funk / Soul », le plan et les verdicts « Funk & Soul »."""
    matched = Classification("a", MatchStatus.MATCHED, genres=("Funk / Soul",), styles=("Soul",))
    assert assign(matched, plan, TAXONOMY).name == "Soul · Soul"


def test_style_spellings_go_through_the_alias_table(plan):
    """« Hip-Hop » et « Hip Hop » désignent le même style."""
    assert assign(judged("a", "Hip Hop", "Hip-Hop"), plan, TAXONOMY).name == "Rap · Hip hop"


def test_a_track_no_rule_accepts_is_left_out(plan):
    assert assign(judged("a", "Rock", "Grunge"), plan, TAXONOMY) is None


def test_a_track_with_nothing_known_is_left_out(plan):
    assert assign(Classification("a", MatchStatus.UNMATCHED), plan, TAXONOMY) is None


def test_playlists_follow_the_file_order_and_empty_ones_are_omitted(plan):
    plans = plan_by_rules(
        [judged("a", "Funk & Soul", "Soul"), judged("b", "Jazz", "Fusion", "Groovy")],
        TAXONOMY, plan, "✱",
    )
    assert [p.name for p in plans] == ["Jazz · Jazz-funk & groove", "Soul · Soul"]
    assert plans[0].key == "plan/jazz-jazz-funk-and-groove"


def test_the_description_states_the_rule_and_carries_the_marker(plan):
    playlist = plan_by_rules([judged("b", "Jazz", "Fusion", "Groovy")], TAXONOMY, plan, "✱")[0]

    assert "Jazz-funk, et la fusion quand elle groove." in playlist.description
    assert "style Jazz-Funk — ou — style Fusion ; ambiance Groovy" in playlist.description
    assert playlist.description.endswith("✱")


def test_sizes_outside_the_target_are_flagged(plan):
    assert "à découper" in plan.size_warning(4)
    assert "à fusionner" in plan.size_warning(1)
    assert plan.size_warning(2) is None


# ------------------------------------------------------------------ lecture


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[[playlist]]\nnom = "X"\ngenres = ["Jaz"]', "genre « Jaz » inconnu"),
        ('[[playlist]]\nnom = "X"\nambiances = ["Nostalgique"]', "ambiance « Nostalgique »"),
        ('[[playlist]]\nnom = "X"\nstyle = ["Trap"]', "clé inconnue « style »"),
        ('[[playlist]]\ngenres = ["Jazz"]', "« nom » manquant"),
        ('[[playlist]]\nnom = "X"\nstyles = "Trap"\n[taille]\nmin = 5\nmax = 2', "min <= max"),
        ("[taille]\nmin = 1", "aucune playlist"),
        ('[[playlist]]\nnom = "X"\nstyles = [3]', "liste de noms"),
        ('[[playlist]]\nnom = "Été"\n[[playlist]]\nnom = "ete"', "casse ou les accents"),
        ("[[playlist]\nnom =", "illisible"),
    ],
)
def test_a_mistake_in_the_file_is_reported_not_silenced(text, message):
    """Une règle mal orthographiée ne correspondrait à rien, en silence."""
    with pytest.raises(PlanError, match=message):
        parse_plan(text, TAXONOMY)


def test_genre_and_mood_names_tolerate_case_and_accents():
    plan = parse_plan('[[playlist]]\nnom = "X"\ngenres = ["hip hop"]\nambiances = ["energique"]',
                      TAXONOMY)
    assert plan.playlists[0].rules[0].genres == ("Hip Hop",)
    assert plan.playlists[0].rules[0].moods == ("Énergique",)


def test_a_missing_plan_file_says_what_to_do(tmp_path):
    with pytest.raises(PlanError, match="introuvable"):
        load_plan(tmp_path / "absent.toml", TAXONOMY)


# ------------------------------------------------------------ plan livré


def test_the_shipped_plan_is_valid():
    plan = load_plan("config/playlists.toml", TAXONOMY)
    assert len(plan.playlists) >= 30


@pytest.mark.parametrize(
    ("genre", "style", "mood", "expected"),
    [
        ("Hip Hop", "Contemporary R&B", "Calme", "Soul · R&B & neo soul posés"),
        ("Electronic", "Deep House", "Planant", "Électro · Deep house planante"),
        ("Jazz", "Fusion", "Groovy", "Jazz · Jazz-funk & groove"),
        ("Jazz", "Bossa Nova", "Calme", "Brésil · Bossa nova"),
        ("Latin", "Samba", "Festif", "Brésil · Samba & groove"),
        ("Folk & World", "Gnawa", "Planant", "Maghreb · Gnawa"),
        ("Folk & World", "Raï", "Festif", "Maghreb · Raï & chaâbi"),
        ("Folk & World", "Tarab", "Mélancolique", "Orient · Fairouz, tarab & chanson"),
        ("Folk & World", "Al Jeel", "Festif", "Orient · Pop arabe"),
        ("Folk & World", "Ethio-Jazz", "Groovy", "Jazz · Ethio-jazz"),
        ("Funk & Soul", "Afrobeat", "Groovy", "Funk · Afrobeat"),
        ("Pop", "Afrobeats", "Festif", "Afrique · Afrobeats & amapiano"),
        ("Jazz", "Latin Jazz", "Groovy", "Jazz · Latin jazz"),
        ("Latin", "Salsa", "Festif", "Latino · Salsa & latin soul"),
        ("Latin", "Flamenco", "Festif", "Folk · Flamenco & tsigane"),
        ("Reggae", "Roots Reggae", "Groovy", "Caraïbes · Reggae, dub & dancehall"),
        ("Pop", "Chanson", "Calme", "Pop · Variété française"),
        ("Classical", "Romantic", "Mélancolique", "Classique · Romantique tendre"),
        ("Electronic", "Glitch", "Cérébral", "Électro · Expérimental & IDM"),
        ("Electronic", "Video Game Music", "Calme", "Électro · Downtempo chill"),
    ],
)
def test_the_shipped_plan_ranks_representative_verdicts(genre, style, mood, expected):
    plan = load_plan("config/playlists.toml", TAXONOMY)
    assert assign(judged("a", genre, style, mood), plan, TAXONOMY).name == expected


@pytest.mark.parametrize(
    ("genre", "style", "mood", "expected"),
    [
        ("Jazz", "Fusion", "Énergique", "Jazz · Fusion énergique"),
        ("Jazz", "Fusion", "Calme", "Jazz · Fusion planante"),
        ("Funk & Soul", "Soul", "Groovy", "Soul · Soul groovy"),
        ("Funk & Soul", "Soul", "Mélancolique", "Soul · Soul ballades"),
        ("Classical", "Baroque", "Énergique", "Classique · Baroque vif"),
        ("Classical", "Baroque", "Calme", "Classique · Baroque lent"),
        ("Electronic", "Techno", "Énergique", "Électro · Techno percutante"),
        ("Electronic", "Techno", "Planant", "Électro · Techno hypnotique"),
        ("Jazz", "Swing", "Calme", "Jazz · Vocal & ballades"),
        ("Jazz", "Vocal", "Groovy", "Jazz · Swing & big band"),
        ("Jazz", "Smooth Jazz", "Calme", "Jazz · Lounge & jazz d'ambiance"),
    ],
)
def test_the_shipped_plan_keeps_one_mood_per_playlist(genre, style, mood, expected):
    """Une même famille, deux ambiances : deux playlists. Lancer « Boom bap
    groovy » ne doit pas tomber sur un titre sombre."""
    plan = load_plan("config/playlists.toml", TAXONOMY)
    assert assign(judged("a", genre, style, mood), plan, TAXONOMY).name == expected


@pytest.mark.parametrize("genre", ["Hip Hop", "Electronic", "Jazz", "Funk & Soul", "Classical",
                                   "Pop", "Rock", "Blues", "Latin", "Folk & World", "Reggae"])
def test_a_track_without_a_known_mood_still_finds_its_family(genre):
    """Un titre que seul Discogs décrit n'a pas d'ambiance : le découpage par
    ambiance ne doit pas le laisser sans playlist."""
    plan = load_plan("config/playlists.toml", TAXONOMY)
    playlist = assign(judged("a", genre, "Style inconnu", None), plan, TAXONOMY)
    assert playlist is not None


def test_the_shipped_plan_has_no_catch_all_named_divers():
    plan = load_plan("config/playlists.toml", TAXONOMY)
    assert not [p.name for p in plan.playlists if "Divers" in p.name]


def test_the_shipped_plan_has_no_catch_all_world_family():
    """« Monde » ne dit rien de ce qu'on va entendre : chaque playlist porte
    une famille réelle — Brésil, Maghreb, Orient, Jazz…"""
    plan = load_plan("config/playlists.toml", TAXONOMY)
    assert not [p.name for p in plan.playlists if p.name.startswith("Monde")]


def test_the_shipped_plan_has_no_soundtrack_playlist():
    """Une musique de film est rangée selon sa musique, pas selon son usage."""
    plan = load_plan("config/playlists.toml", TAXONOMY)
    assert not [p.name for p in plan.playlists if p.name.startswith("BO")]


@pytest.mark.parametrize(
    ("artist", "style", "expected"),
    [
        ("Fairuz", "Ballad", "Orient · Fairouz, tarab & chanson"),
        ("Munir Bachir", "Traditional", "Orient · Oud & taqsim"),
        ("Abdou El Omari", "Instrumental", "Funk · Funk arabe des années 70"),
        ("Compagnie El Hamri", "Traditional", "Maghreb · Traditions"),
        ("Amadou & Mariam", "Folk", "Afrique · Folk, blues & mandingue"),
        ("Simon & Garfunkel", "Folk", "Folk · Folk acoustique & traditions"),
    ],
)
def test_the_shipped_plan_places_world_music_by_region(artist, style, expected):
    """« Ballad » ou « Folk » ne disent pas d'où vient une musique : l'artiste, si."""
    plan = load_plan("config/playlists.toml", TAXONOMY)
    playlist = assign(judged("a", "Folk & World", style), plan, TAXONOMY, (artist,))
    assert playlist.name == expected


# ---------------------------------------------------------------- artistes


def test_an_artist_rule_matches_guests_and_youtube_channels():
    plan = parse_plan('[[playlist]]\nnom = "Orient"\nartistes = ["Ziad Rahbani"]\n'
                      '[[playlist]]\nnom = "Reste"', TAXONOMY)
    as_guest = assign(judged("a", "Folk & World", "Ballad"), plan, TAXONOMY,
                      ("Fairuz", "Ziad Rahbani"))
    as_channel = assign(judged("b", "Folk & World", "Ballad"), plan, TAXONOMY,
                        ("Ziad Rahbani - Topic",))
    other = assign(judged("c", "Folk & World", "Ballad"), plan, TAXONOMY, ("Fairuz",))

    assert (as_guest.name, as_channel.name, other.name) == ("Orient", "Orient", "Reste")


def test_an_artist_rule_combines_with_the_other_criteria():
    plan = parse_plan('[[playlist]]\nnom = "X"\nartistes = ["Fela Kuti"]\ngenres = ["Funk & Soul"]',
                      TAXONOMY)
    assert assign(judged("a", "Funk & Soul", "Afrobeat"), plan, TAXONOMY, ("Fela Kuti",))
    assert assign(judged("b", "Jazz", "Afrobeat"), plan, TAXONOMY, ("Fela Kuti",)) is None


def test_artists_named_but_absent_from_the_library_are_reported():
    plan = parse_plan('[[playlist]]\nnom = "X"\nartistes = ["Fairuz", "Fairouzz"]', TAXONOMY)
    assert plan.unknown_artists([("Fairuz",), ("Warda",)]) == ["Fairouzz"]


# ------------------------------------------------------------------- aperçu


@pytest.fixture
def library(repository, config, tmp_path):
    """Une bibliothèque, ses verdicts dans le fichier, et le plan de test."""
    path = tmp_path / "playlists.toml"
    path.write_text(PLAN, encoding="utf-8")
    config.taxonomy.playlists_file = str(path)
    tracks = [
        Track("j1", "Chameleon", ("Herbie Hancock",), "Head Hunters"),
        Track("j2", "Actual Proof", ("Herbie Hancock",), "Thrust"),
        Track("r1", "Lithium", ("Nirvana",), "Nevermind"),
    ]
    repository.upsert_tracks(tracks)
    classify_tracks(tracks, repository, FakeDiscogs({
        "Nirvana": [make_candidate(1, "Nevermind", "Nirvana")],
    }), config)
    verdicts.save(VerdictBook([
        Verdict("Herbie Hancock", "Chameleon", "Jazz", "Jazz-Funk", "Groovy", 0.9, note="Groove."),
        Verdict("Herbie Hancock", "Actual Proof", "Jazz", "Fusion", "Groovy", 0.2,
                note="Peu documenté."),
    ]), config.claude.verdicts_file)
    return repository


def test_the_preview_reads_the_verdicts_file_without_a_new_analysis(library, config):
    """Les verdicts ont été écrits après l'analyse : l'aperçu les prend quand même."""
    summary, _, _ = build_preview(library, apply_sort_mode(config, "familles"), sort_mode="familles")
    groove = next(p for p in summary.playlists if p.name == "Jazz · Jazz-funk & groove")

    assert groove.count == 2
    assert groove.criteria == ["style Jazz-Funk", "style Fusion ; ambiance Groovy"]
    assert groove.note == "Jazz-funk, et la fusion quand elle groove."


def test_unsure_verdicts_are_applied_but_flagged(library, config):
    summary, _, _ = build_preview(library, apply_sort_mode(config, "familles"), sort_mode="familles")
    groove = next(p for p in summary.playlists if p.name == "Jazz · Jazz-funk & groove")
    unsure = next(t for t in groove.tracks if t.title == "Actual Proof")

    assert unsure.unsure and unsure.note == "Peu documenté." and unsure.mood == "Groovy"
    assert groove.unsure == 1


def test_a_track_no_rule_accepts_is_listed_with_what_it_is(library, config):
    """Nirvana (Rock / Grunge) n'a pas de playlist dans ce plan : il doit être
    montré, avec de quoi écrire la règle qui manque."""
    summary, _, _ = build_preview(library, apply_sort_mode(config, "familles"), sort_mode="familles")
    left = next(t for t in summary.unsorted if t["video_id"] == "r1")

    assert left["reason"] == "no_rule"
    assert left["detail"] == "Rock / Grunge"
    assert summary.reason_labels["no_rule"] == "aucune playlist du plan ne l'accepte"


def smaller_than(config, minimum):
    path = config.taxonomy.playlists_file
    with open(path, "r+", encoding="utf-8") as file:
        text = file.read().replace("min = 2", f"min = {minimum}")
        file.seek(0)
        file.write(text)


def test_the_size_warning_reaches_the_preview(library, config):
    smaller_than(config, 3)
    summary, _, _ = build_preview(library, apply_sort_mode(config, "familles"), sort_mode="familles")
    groove = next(p for p in summary.playlists if p.name == "Jazz · Jazz-funk & groove")

    assert "à fusionner" in groove.size_warning


def test_the_cli_plan_shows_size_warnings(library, config, monkeypatch, capsys):
    from ytmgc.enrich import apply_verdicts

    smaller_than(config, 3)
    monkeypatch.setattr(cli, "_repository", lambda _config: library)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    apply_verdicts(library, verdicts.load(config.claude.verdicts_file), TAXONOMY, config)

    assert cli.main(["plan", "--tri", "familles"]) == 0
    assert "2  Jazz · Jazz-funk & groove   ⚠ petite" in capsys.readouterr().out


def test_a_mistake_in_the_plan_reaches_the_page_as_a_readable_error(repository, config, tmp_path):
    from fastapi.testclient import TestClient
    from fakes import FakePlaylistClient

    from ytmgc.web.app import Services, create_app
    from ytmgc.web.jobs import JobRunner

    path = tmp_path / "playlists.toml"
    path.write_text('[[playlist]]\nnom = "X"\ngenres = ["Jaz"]', encoding="utf-8")
    config.taxonomy.playlists_file = str(path)
    client = TestClient(create_app(Services(
        config=config, repository=repository,
        youtube_factory=lambda _c: FakePlaylistClient(),
        discogs_factory=lambda _c: FakeDiscogs({}), jobs=JobRunner(),
    )))

    response = client.post("/api/preview", json={"sort_mode": "familles"})
    assert response.status_code == 400
    assert "genre « Jaz » inconnu" in response.json()["detail"]


def test_the_preview_reports_artists_missing_from_the_library(library, config):
    with open(config.taxonomy.playlists_file, "a", encoding="utf-8") as file:
        file.write('\n[[playlist]]\nnom = "Orient"\nartistes = ["Herbie Hancock", "Fairouzz"]\n')
    summary, _, _ = build_preview(library, apply_sort_mode(config, "familles"), sort_mode="familles")
    assert summary.unknown_artists == ["Fairouzz"]


# -------------------------------------------------------------------- pays


@pytest.mark.parametrize(
    ("artist", "expected"),
    [
        ("Nekfeu", "Rap · FR sombre"),
        ("Damso", "Rap · FR sombre"),
        ("Kendrick Lamar", "Rap · US"),
        ("Knucks", "Rap · UK"),
        ("ElGrandeToto", "Rap · Maghreb"),
        ("Pashanim", "Rap · Ailleurs"),
        ("Personne De Connu", "Rap · Pays à préciser"),
    ],
)
def test_the_shipped_plan_sorts_rap_by_country(artist, expected):
    """Le rap se range par pays : on n'écoute pas Nekfeu et Kendrick Lamar
    dans la même playlist. Le pays vient de data/pays.txt."""
    from ytmgc import origins

    plan = load_plan("config/playlists.toml", TAXONOMY)
    known = origins.load("data/pays.txt")
    playlist = assign(judged("a", "Hip Hop", "Trap", "Sombre"), plan, TAXONOMY, (artist,), known)
    assert playlist.name == expected


def test_a_country_rule_matches_any_artist_of_the_track():
    plan = parse_plan('[[playlist]]\nnom = "FR"\npays = ["FR"]', TAXONOMY)
    assert assign(judged("a", "Hip Hop", "Trap"), plan, TAXONOMY,
                  ("Kendrick Lamar", "Nekfeu"), {"nekfeu": "FR", "kendrick lamar": "US"})


def test_the_question_mark_keeps_tracks_whose_country_is_unknown():
    plan = parse_plan('[[playlist]]\nnom = "FR"\npays = ["FR"]\n'
                      '[[playlist]]\nnom = "Attente"\npays = ["?"]', TAXONOMY)
    origins = {"nekfeu": "FR", "kendrick lamar": "US"}
    assert assign(judged("a", "Hip Hop", "Trap"), plan, TAXONOMY, ("Inconnu",), origins).name == "Attente"
    assert assign(judged("b", "Hip Hop", "Trap"), plan, TAXONOMY, ("Kendrick Lamar",), origins) is None


def test_a_malformed_country_code_is_refused():
    with pytest.raises(PlanError, match="pays « FRANCE »"):
        parse_plan('[[playlist]]\nnom = "X"\npays = ["France"]', TAXONOMY)


def test_the_country_file_is_read_tolerantly(tmp_path):
    from ytmgc import origins

    path = tmp_path / "pays.txt"
    path.write_text("# commentaire\nNekfeu\tfr\nbancale\nKendrick Lamar - Topic\tUS\nX\tFrance\n",
                    encoding="utf-8")
    assert origins.load(path) == {"nekfeu": "FR", "kendrick lamar": "US"}


def test_the_shipped_country_file_covers_the_main_rappers():
    from ytmgc import origins

    known = origins.load("data/pays.txt")
    assert (known["nekfeu"], known["kendrick lamar"], known["damso"]) == ("FR", "US", "BE")


@pytest.mark.parametrize(
    ("mood", "expected"),
    [
        ("Énergique", "Rap · FR énergique"),
        ("Festif", "Rap · FR énergique"),
        ("Sombre", "Rap · FR sombre"),
        ("Groovy", "Rap · FR groovy & posé"),
        ("Calme", "Rap · FR groovy & posé"),
        ("Mélancolique", "Rap · FR mélancolique"),
        (None, "Rap · FR mélancolique"),
    ],
)
def test_french_rap_is_also_split_by_mood(mood, expected):
    """Le rap francophone, de loin le plus fourni, est découpé par ambiance ;
    US et UK restent d'un seul tenant."""
    from ytmgc import origins

    plan = load_plan("config/playlists.toml", TAXONOMY)
    known = origins.load("data/pays.txt")
    playlist = assign(judged("a", "Hip Hop", "Trap", mood), plan, TAXONOMY, ("Nekfeu",), known)
    assert playlist.name == expected


# ------------------------------------------------------- playlists manuelles


def test_a_manual_playlist_has_no_rule_and_takes_nothing_by_itself():
    plan = parse_plan('[[playlist]]\nnom = "Rap · Drill UK"\nmanuelle = true\n'
                      '[[playlist]]\nnom = "Rap"\ngenres = ["Hip Hop"]', TAXONOMY)
    drill = plan.playlists[0]

    assert drill.rules == ()
    assert assign(judged("a", "Hip Hop", "Drill"), plan, TAXONOMY).name == "Rap"


def test_a_manual_playlist_receives_the_tracks_moved_into_it():
    plan = parse_plan('[[playlist]]\nnom = "Dimanche"\nmanuelle = true', TAXONOMY)
    plans = plan_by_rules([judged("a", "Jazz", "Fusion")], TAXONOMY, plan, "✱",
                          {"a": "plan/dimanche"})
    assert [(p.name, p.video_ids) for p in plans] == [("Dimanche", ("a",))]
    assert "Playlist composée à la main." in plans[0].description


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[[playlist]]\nnom = "X"\nmanuelle = true\ngenres = ["Jazz"]', "pas de critère"),
        ('[[playlist]]\nnom = "X"\nmanuelle = "oui"', "true ou false"),
    ],
)
def test_a_manual_playlist_is_checked_too(text, message):
    with pytest.raises(PlanError, match=message):
        parse_plan(text, TAXONOMY)


def test_creating_a_playlist_appends_it_and_keeps_the_rest_of_the_file(tmp_path):
    from ytmgc.playlist_plan import MANUAL_SECTION, add_manual_playlist

    path = tmp_path / "playlists.toml"
    path.write_text(PLAN.replace("[taille]", "# Mon commentaire\n[taille]"), encoding="utf-8")
    made = add_manual_playlist(path, '  Rap ·  Drill "UK" ', "Pour la salle", TAXONOMY)
    add_manual_playlist(path, "Dimanche", "", TAXONOMY)
    text = path.read_text(encoding="utf-8")

    assert made.name == 'Rap · Drill "UK"' and made.description == "Pour la salle"
    assert "# Mon commentaire" in text and text.count(MANUAL_SECTION) == 1
    names = [p.name for p in load_plan(path, TAXONOMY).playlists]
    assert names[-2:] == ['Rap · Drill "UK"', "Dimanche"]


@pytest.mark.parametrize(
    ("name", "message"),
    [("Jazz · fusion", "existe déjà"), ("   ", "Donne un nom"), ("x" * 151, "trop long")],
)
def test_a_new_playlist_name_must_be_free_and_reasonable(tmp_path, name, message):
    from ytmgc.playlist_plan import add_manual_playlist

    path = tmp_path / "playlists.toml"
    path.write_text(PLAN, encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(PlanError, match=message):
        add_manual_playlist(path, name, "", TAXONOMY)
    assert path.read_text(encoding="utf-8") == before
