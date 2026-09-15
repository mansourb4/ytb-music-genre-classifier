"""Ce qui part vers le modèle, et ce qu'on accepte d'en recevoir.

Aucune requête réseau : ces fonctions sont pures, et ce sont elles qui
décident de ce qu'on paie et de ce qu'on croit.
"""

import json

import pytest

from ytmgc.config import ClaudeConfig
from ytmgc.models import Classification, MatchStatus, Track
from ytmgc.sources.claude import (
    Subject,
    build_prompt,
    build_reference,
    chunk,
    estimate,
    parse_verdicts,
)
from ytmgc.taxonomy import load_taxonomy

SUBJECTS = [
    Subject("Nirvana", "Something In The Way", "Nevermind", 1991, ("Rock",), ("Grunge",),
            ("grunge", "90s")),
    Subject("Aphex Twin", "Xtal", "Selected Ambient Works", 1992, ("Electronic",), ("Ambient",)),
]


def answer(*items) -> str:
    return json.dumps({"verdicts": list(items)})


def verdict(n=1, genre="Rock", style="Acoustic", mood="Mélancolique", confidence=0.9, note="Ok."):
    return {"n": n, "genre": genre, "style": style, "mood": mood,
            "confidence": confidence, "note": note}


# ------------------------------------------------------------------ envoi


def test_a_subject_carries_the_hints_without_hiding_the_track():
    line = SUBJECTS[0].line(1)

    assert line.startswith("1. Nirvana – Something In The Way")
    assert "Nevermind" in line and "1991" in line
    assert "Discogs : Rock / Grunge" in line
    assert "Last.fm : grunge, 90s" in line


def test_a_subject_without_metadata_is_still_submittable():
    bare = Subject("Inconnu", "Titre")
    assert bare.line(3) == "3. Inconnu – Titre"


def test_the_prompt_numbers_every_track():
    prompt = build_prompt(SUBJECTS)

    assert "Titres à juger (2)" in prompt
    assert "1. Nirvana" in prompt and "2. Aphex Twin" in prompt


def test_the_reference_carries_the_whole_vocabulary():
    """Le modèle doit répondre « à l'identique » : le vocabulaire est donc
    transmis, et c'est ce bloc qui est mis en cache."""
    reference = build_reference(load_taxonomy())

    assert "Mélancolique" in reference and "Groovy" in reference
    assert "Deep House" in reference
    assert "Funk & Soul" in reference


def test_subjects_are_split_into_requests():
    parts = chunk(list(range(10)), 3)
    assert [len(part) for part in parts] == [3, 3, 3, 1]


def test_a_subject_survives_the_pending_file():
    restored = Subject.from_dict(SUBJECTS[0].to_dict())
    assert restored == SUBJECTS[0]


def test_a_subject_is_built_from_a_track_and_its_classification():
    track = Track("v1", "Lithium", ("Nirvana",), "Nevermind")
    classification = Classification(
        "v1", MatchStatus.MATCHED, genres=("Rock",), styles=("Grunge",), year=1991,
        tags=("grunge", "90s"),
    )
    subject = Subject.of(track, classification)

    assert (subject.artist, subject.title, subject.year) == ("Nirvana", "Lithium", 1991)
    assert subject.tags == ("grunge", "90s")


# --------------------------------------------------------------- réception


def test_a_verdict_is_attached_to_its_track_by_number():
    parsed = parse_verdicts(answer(verdict(n=2, style="Ambient Techno")), SUBJECTS)

    assert len(parsed) == 1
    assert parsed[0].title == "Xtal"
    assert parsed[0].style == "Ambient Techno"


def test_a_verdict_pointing_nowhere_is_dropped():
    """Mal rattacher un verdict serait pire que d'en manquer un : il serait
    ensuite tenu pour acquis, et jamais redemandé."""
    assert parse_verdicts(answer(verdict(n=9)), SUBJECTS) == []
    assert parse_verdicts(answer(verdict(n=0)), SUBJECTS) == []
    assert parse_verdicts(answer(verdict(n="deux")), SUBJECTS) == []


def test_a_number_returned_twice_counts_once():
    parsed = parse_verdicts(answer(verdict(n=1), verdict(n=1, style="Folk")), SUBJECTS)

    assert [v.style for v in parsed] == ["Acoustic"]


def test_an_unparsable_answer_yields_nothing_rather_than_raising():
    assert parse_verdicts("pas du JSON", SUBJECTS) == []
    assert parse_verdicts("", SUBJECTS) == []
    assert parse_verdicts(json.dumps({"verdicts": None}), SUBJECTS) == []
    assert parse_verdicts(json.dumps({"verdicts": ["texte"]}), SUBJECTS) == []


def test_confidence_is_clamped_and_defaults_to_zero():
    parsed = parse_verdicts(
        answer(verdict(n=1, confidence=7), verdict(n=2, confidence="beaucoup")), SUBJECTS
    )

    assert [v.confidence for v in parsed] == [1.0, 0.0]


def test_a_parsed_verdict_keeps_the_note_for_the_reader():
    parsed = parse_verdicts(answer(verdict(n=1, note="Berceuse sépulcrale.")), SUBJECTS)
    assert parsed[0].note == "Berceuse sépulcrale."


# ------------------------------------------------------------------ coût


def test_the_cost_is_announced_before_being_spent():
    approximate = estimate(2500, ClaudeConfig())

    assert approximate.requests == 100
    assert 1 < approximate.dollars < 20
    assert "$" in approximate.line()


def test_the_batch_api_is_half_price():
    config = ClaudeConfig()
    assert estimate(1000, config).dollars < estimate(1000, config, batched=False).dollars


def test_judging_nothing_costs_nothing():
    assert estimate(0, ClaudeConfig()).dollars == 0.0


# ------------------------------------------------- conformité au SDK officiel

anthropic = pytest.importorskip("anthropic")


def client():
    from ytmgc.sources.claude import ClaudeClient

    return ClaudeClient(ClaudeConfig(api_key="clé-de-test"), load_taxonomy())


def test_a_request_is_accepted_by_the_official_sdk_shape():
    """La passe est facturée : une requête mal formée se découvrirait au pire
    moment. Ce test la construit avec les types réels du SDK."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    params = client()._params(SUBJECTS)
    request = Request(custom_id="lot-0", params=MessageCreateParamsNonStreaming(**params))

    assert request["custom_id"] == "lot-0"
    assert request["params"]["model"] == "claude-opus-5"
    assert request["params"]["output_config"]["format"]["type"] == "json_schema"


def test_the_shared_preamble_is_marked_for_caching():
    """Le vocabulaire est identique à chaque requête : le répéter cent fois à
    plein tarif doublerait la facture."""
    system = client()._params(SUBJECTS)["system"]

    assert [block.get("cache_control") for block in system][-1] == {"type": "ephemeral"}


def test_the_messages_api_accepts_every_parameter_we_send():
    import inspect

    signature = inspect.signature(anthropic.Anthropic(api_key="x").messages.create)
    assert set(client()._params(SUBJECTS)) <= set(signature.parameters)


def test_a_missing_key_is_refused_before_any_request():
    from ytmgc.sources.claude import ClaudeClient, ClaudeError

    with pytest.raises(ClaudeError, match="ANTHROPIC_API_KEY"):
        ClaudeClient(ClaudeConfig(api_key=""), load_taxonomy())
