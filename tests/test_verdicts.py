"""Le fichier de verdicts : le seul résultat de l'outil qui se paie.

Il doit donc survivre à tout — une base effacée, une interruption en pleine
écriture, une ligne corrigée à la main — et ne jamais faire redemander un titre
déjà jugé.
"""

from ytmgc import verdicts
from ytmgc.models import Track
from ytmgc.verdicts import MANUAL, MODEL, Verdict, VerdictBook

NIRVANA = Verdict(
    artist="Nirvana", title="Something In The Way", genre="Rock", style="Grunge",
    mood="Mélancolique", confidence=0.9, note="Berceuse sépulcrale, voix au bord du souffle.",
)
APHEX = Verdict(
    artist="Aphex Twin", title="Xtal", genre="Electronic", style="Ambient Techno",
    mood="Planant", confidence=0.85, note="Nappes répétées, rythme effacé.",
)


def test_a_verdict_survives_a_round_trip(tmp_path):
    path = tmp_path / "verdicts.txt"
    verdicts.save(VerdictBook([NIRVANA, APHEX]), path)
    book = verdicts.load(path)

    assert len(book) == 2
    assert book.get(NIRVANA.key) == NIRVANA


def test_the_file_is_readable_and_editable_by_hand(tmp_path):
    """Le fichier est fait pour être ouvert : il s'explique, et ses colonnes
    se lisent sans outil."""
    path = tmp_path / "verdicts.txt"
    verdicts.save(VerdictBook([NIRVANA]), path)
    text = path.read_text(encoding="utf-8")

    assert text.startswith("#")
    assert "ambiance" in text
    body = [line for line in text.splitlines() if not line.startswith("#") and line.strip()]
    assert body == ["Nirvana\tSomething In The Way\tRock\tGrunge\tMélancolique\t0.90\tclaude\t"
                    "Berceuse sépulcrale, voix au bord du souffle."]


def test_a_hand_written_line_is_read_back(tmp_path):
    path = tmp_path / "verdicts.txt"
    path.write_text("Daft Punk\tVeridis Quo\tElectronic\tFrench House\tPlanant\t1\tmanuel\t\n")
    book = verdicts.load(path)

    verdict = book.get(verdicts.entry_key("Daft Punk", "Veridis Quo"))
    assert verdict.style == "French House"
    assert verdict.source == MANUAL
    assert verdict.confidence == 1.0


def test_a_short_hand_written_line_still_counts(tmp_path):
    """Écrire cinq colonnes à la main doit suffire : la confiance et la note
    ne sont pas ce qui range un titre."""
    path = tmp_path / "verdicts.txt"
    path.write_text("Miles Davis\tSo What\tJazz\tModal\tCérébral\n")

    verdict = verdicts.load(path).get(verdicts.entry_key("Miles Davis", "So What"))
    assert (verdict.genre, verdict.style, verdict.mood) == ("Jazz", "Modal", "Cérébral")


def test_a_broken_line_does_not_void_the_others(tmp_path):
    path = tmp_path / "verdicts.txt"
    path.write_text(
        "# commentaire\n"
        "\n"
        "n'importe quoi\n"
        "\t\tRock\tGrunge\tSombre\n"
        "Nirvana\tLithium\tRock\tGrunge\tÉnergique\t0.8\tclaude\t\n"
    )
    book = verdicts.load(path)

    assert len(book) == 1
    assert book.get(verdicts.entry_key("Nirvana", "Lithium")) is not None


def test_the_model_never_overwrites_a_human_decision():
    """Une ligne corrigée à la main est le dernier mot : une nouvelle passe ne
    doit pas la balayer sans que rien n'ait été demandé."""
    mine = verdicts.manual(Verdict("Nirvana", "Something In The Way", "Rock", "Acoustic", "Calme"))
    book = VerdictBook([mine])
    book.add(NIRVANA)

    assert book.get(mine.key).style == "Acoustic"
    assert book.get(mine.key).source == MANUAL


def test_a_human_decision_does_overwrite_the_model():
    book = VerdictBook([NIRVANA])
    book.add(verdicts.manual(Verdict("Nirvana", "Something In The Way", "Rock", "Folk", "Calme")))

    assert book.get(NIRVANA.key).style == "Folk"


def test_two_editions_of_a_track_share_one_verdict():
    """La clé ignore l'album et la ponctuation : un même morceau ne se paie pas
    deux fois parce qu'il figure aussi sur une compilation."""
    book = VerdictBook([NIRVANA])
    live = Track("v1", "Something in the Way", ("Nirvana",), "MTV Unplugged")

    assert book.for_track(live) is not None


def test_known_tracks_are_never_resubmitted():
    book = VerdictBook([NIRVANA])
    library = [
        Track("v1", "Something In The Way", ("Nirvana",), "Nevermind"),
        Track("v2", "Lithium", ("Nirvana",), "Nevermind"),
        Track("v3", "Lithium", ("Nirvana",), "Nevermind (Deluxe)"),
    ]

    pending = book.missing(library)
    assert [track.video_id for track in pending] == ["v2"]


def test_a_tab_inside_a_field_cannot_break_the_columns():
    verdict = Verdict("A\tB", "Ti\ttre", "Rock", "Punk", "Énergique", 0.5, MODEL, "no\tte")
    assert verdict.line().count("\t") == len(verdicts.COLUMNS) - 1


def test_saving_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / "verdicts.txt"
    verdicts.save(VerdictBook([NIRVANA]), path)

    assert [p.name for p in tmp_path.iterdir()] == ["verdicts.txt"]


def test_the_file_is_sorted_so_two_versions_compare(tmp_path):
    path = tmp_path / "verdicts.txt"
    verdicts.save(VerdictBook([NIRVANA, APHEX]), path)
    body = [line for line in path.read_text(encoding="utf-8").splitlines() if line[:1] != "#"]

    assert body[0].startswith("Aphex Twin")
