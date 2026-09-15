"""Les deux commandes qui dépensent, vues depuis le terminal.

C'est de là que l'utilisateur lancera la passe : le garde-fou doit tenir ici
aussi, pas seulement dans l'interface web.
"""

import pytest
from fakes import FakeJudge

from ytmgc import cli, verdicts
from ytmgc.models import Track
from ytmgc.store import Repository, connect
from ytmgc.verdicts import Verdict, VerdictBook

LIBRARY = [
    Track("g1", "Something In The Way", ("Nirvana",), "Nevermind"),
    Track("g2", "Lithium", ("Nirvana",), "Nevermind"),
]
ANSWERS = {
    "Something In The Way": Verdict(
        "Nirvana", "Something In The Way", "Rock", "Acoustic", "Mélancolique", 0.9,
        note="Berceuse sépulcrale.",
    ),
    "Lithium": Verdict("Nirvana", "Lithium", "Rock", "Grunge", "Énergique", 0.85),
}


@pytest.fixture
def judged(config, monkeypatch, tmp_path):
    """Un dépôt peuplé, un juge en mémoire, et le CLI branché sur les deux."""
    config.store.path = str(tmp_path / "cli.db")
    repository = Repository(connect(config.store.path))
    repository.upsert_tracks(LIBRARY)

    judge = FakeJudge(ANSWERS)
    monkeypatch.setattr(cli, "_judge", lambda _config: judge)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    return judge


def run(*argv) -> int:
    return cli.main(list(argv))


def test_dry_run_announces_the_cost_without_sending_anything(judged, capsys):
    assert run("enrich", "--dry-run") == 0

    out = capsys.readouterr().out
    assert "2 titre(s) à juger" in out
    assert "$" in out
    assert judged.submitted == []


def test_the_pass_refuses_to_spend_without_an_answer(judged, capsys, monkeypatch):
    """Une réponse vide, ou un terminal sans entrée, ne doit jamais valoir oui."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert run("enrich") == 0

    assert "Abandon" in capsys.readouterr().out
    assert judged.submitted == []


def test_a_confirmed_pass_judges_and_writes_the_file(judged, config, capsys):
    assert run("enrich", "--yes") == 0

    out = capsys.readouterr().out
    assert "2 verdict(s) récupéré(s)" in out
    assert len(verdicts.load(config.claude.verdicts_file)) == 2


def test_a_second_pass_finds_nothing_left_to_pay_for(judged, capsys):
    run("enrich", "--yes")
    capsys.readouterr()
    assert run("enrich", "--yes") == 0

    assert "déjà un verdict" in capsys.readouterr().out
    assert len(judged.submitted) == 1


def test_a_deposited_batch_can_be_collected_later(judged, config, capsys):
    """Le lot vit chez Anthropic : on doit pouvoir rendre la main et revenir."""
    assert run("enrich", "--yes", "--no-wait") == 0
    assert "--resume" in capsys.readouterr().out

    assert run("enrich", "--resume") == 0
    assert len(verdicts.load(config.claude.verdicts_file)) == 2
    assert len(judged.submitted) == 1


def test_lookup_judges_one_track_and_keeps_it(judged, config, capsys):
    assert run("lookup", "Nirvana", "Lithium") == 0

    out = capsys.readouterr().out
    assert "Grunge" in out and "Énergique" in out
    assert len(verdicts.load(config.claude.verdicts_file)) == 1


def test_lookup_of_a_known_track_costs_nothing(judged, config, capsys):
    verdicts.save(VerdictBook([ANSWERS["Lithium"]]), config.claude.verdicts_file)
    assert run("lookup", "Nirvana", "Lithium") == 0

    assert "rien n'a été facturé" in capsys.readouterr().out


def test_lookup_reports_a_track_the_model_does_not_know(judged, capsys):
    assert run("lookup", "Personne", "Rien") == 1
    assert "rien rendu d'exploitable" in capsys.readouterr().out
