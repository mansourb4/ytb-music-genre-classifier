"""La passe gratuite : un paquet collé dans Claude.ai, une réponse recollée.

Rien ne contraint ici la forme de la réponse. Le danger n'est pas le verdict
manquant — son titre revient au paquet suivant — mais le verdict mal rattaché,
écrit sous le titre d'un autre morceau puis tenu pour acquis. Ces tests portent
surtout là-dessus.
"""

import json

import pytest

from ytmgc import verdicts
from ytmgc.chat import (
    ChatError,
    build_message,
    import_answer,
    load_packet,
    make_packet,
    prepare,
    read_answer,
)
from ytmgc.models import Track
from ytmgc.sources.claude import Subject
from ytmgc.taxonomy import load_taxonomy
from ytmgc.verdicts import Verdict, VerdictBook

TAXONOMY = load_taxonomy()
SUBJECTS = [
    Subject("Nirvana", "Something In The Way", "Nevermind", 1991),
    Subject("Daft Punk", "Get Lucky", "RAM", 2013, featuring=("Pharrell Williams",)),
    Subject("Radiohead", "Creep", "Pablo Honey", 1993),
]
PACKET = make_packet(SUBJECTS)


def item(n, titre, genre="Rock", style="Grunge", mood="Mélancolique", confidence=0.9, note="…"):
    return {"n": n, "titre": titre, "genre": genre, "style": style, "mood": mood,
            "confidence": confidence, "note": note}


def answer(*items, paquet=PACKET.id, fenced=True) -> str:
    body = json.dumps({"paquet": paquet, "verdicts": list(items)}, ensure_ascii=False)
    return f"Voici :\n```json\n{body}\n```\nBonne écoute." if fenced else body


GOOD = [
    item(1, "Something In The Way", style="Acoustic"),
    item(2, "Get Lucky", genre="Electronic", style="Disco", mood="Festif"),
    item(3, "Creep", style="Alternative Rock"),
]


# ------------------------------------------------------------------- paquet


def test_a_packet_id_depends_on_its_content_only():
    """Préparer deux fois le même paquet doit redonner le même identifiant :
    la réponse obtenue avec la première copie reste importable."""
    assert make_packet(SUBJECTS).id == PACKET.id
    assert make_packet(SUBJECTS[:2]).id != PACKET.id


def test_the_message_carries_instructions_vocabulary_and_numbered_titles():
    message = build_message(PACKET, TAXONOMY)

    assert "musicologue" in message
    assert PACKET.id in message
    assert "Mélancolique" in message and "Deep House" in message
    assert "1. Nirvana – Something In The Way" in message
    assert "3. Radiohead – Creep" in message
    assert '"titre"' in message  # le titre recopié est exigé dès la consigne


# ----------------------------------------------------------------- lecture


def test_a_clean_answer_is_read_whole():
    reading = read_answer(answer(*GOOD), PACKET, TAXONOMY)

    assert [v.title for v in reading.verdicts] == ["Something In The Way", "Get Lucky", "Creep"]
    assert reading.verdicts[1].mood == "Festif"
    assert reading.rejected == []


def test_a_verdict_is_filed_under_the_main_artist():
    """Le verdict doit retrouver son titre, invités ou non."""
    reading = read_answer(answer(*GOOD), PACKET, TAXONOMY)
    track = Track("v1", "Get Lucky", ("Daft Punk", "Pharrell Williams"))

    assert VerdictBook(reading.verdicts).for_track(track) is not None


def test_a_shifted_numbering_is_caught_by_the_echoed_title():
    """Le cas à proprement parler dangereux : Claude saute un numéro, et le
    verdict de « Creep » arrive au n°2, celui de « Get Lucky »."""
    reading = read_answer(answer(GOOD[0], item(2, "Creep")), PACKET, TAXONOMY)

    assert [v.title for v in reading.verdicts] == ["Something In The Way"]
    assert "numérotation décalée" in reading.rejected[0]


def test_a_verdict_without_its_title_cannot_be_checked_and_is_set_aside():
    bare = dict(GOOD[2])
    del bare["titre"]
    reading = read_answer(answer(bare), PACKET, TAXONOMY)

    assert reading.verdicts == []
    assert "titre non recopié" in reading.rejected[0]


def test_an_echoed_title_may_drop_the_editorial_noise():
    """Claude recopie volontiers le titre sans « (Official Video) » : ce n'est
    pas un décalage."""
    packet = make_packet([Subject("Nirvana", "Lithium (Official Music Video)")])
    text = answer(item(1, "Lithium"), paquet=packet.id)

    assert len(read_answer(text, packet, TAXONOMY).verdicts) == 1


def test_a_mood_outside_the_list_is_refused():
    """Une ambiance inventée ouvrirait une neuvième playlist."""
    reading = read_answer(answer(item(1, "Something In The Way", mood="Nostalgique")),
                          PACKET, TAXONOMY)

    assert reading.verdicts == []
    assert "Nostalgique" in reading.rejected[0]


def test_a_genre_outside_the_list_is_refused():
    reading = read_answer(answer(item(1, "Something In The Way", genre="Grunge")),
                          PACKET, TAXONOMY)
    assert reading.verdicts == [] and "genre" in reading.rejected[0]


def test_case_and_accents_do_not_cost_a_verdict():
    reading = read_answer(answer(item(1, "Something In The Way", genre="rock",
                                      mood="melancolique")), PACKET, TAXONOMY)

    assert (reading.verdicts[0].genre, reading.verdicts[0].mood) == ("Rock", "Mélancolique")


def test_an_answer_to_another_packet_is_refused_whole():
    with pytest.raises(ChatError, match="paquet"):
        read_answer(answer(*GOOD, paquet="00000000"), PACKET, TAXONOMY)


def test_an_answer_split_by_continue_is_read_across_blocks():
    """Une réponse longue arrive en plusieurs messages, souvent collés d'un coup."""
    text = answer(GOOD[0]) + "\n\ncontinue\n\n" + answer(GOOD[1], GOOD[2])
    assert len(read_answer(text, PACKET, TAXONOMY).verdicts) == 3


def test_an_answer_without_code_fences_is_still_read():
    assert len(read_answer(answer(*GOOD, fenced=False), PACKET, TAXONOMY).verdicts) == 3


def test_a_bare_list_is_accepted():
    text = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    assert len(read_answer(text, PACKET, TAXONOMY).verdicts) == 3


def test_a_number_given_twice_counts_once():
    reading = read_answer(answer(GOOD[0], item(1, "Something In The Way", style="Folk")),
                          PACKET, TAXONOMY)
    assert [v.style for v in reading.verdicts] == ["Acoustic"]


def test_a_number_outside_the_packet_is_reported():
    reading = read_answer(answer(item(9, "Creep")), PACKET, TAXONOMY)
    assert reading.verdicts == [] and "hors du paquet" in reading.rejected[0]


def test_text_without_any_json_explains_how_to_copy():
    with pytest.raises(ChatError, match="Copier"):
        read_answer("Bien sûr, voici mon analyse de ces morceaux…", PACKET, TAXONOMY)


# -------------------------------------------------------- de bout en bout


LIBRARY = [
    Track("g1", "Something In The Way", ("Nirvana",), "Nevermind"),
    Track("g2", "Get Lucky", ("Daft Punk", "Pharrell Williams"), "RAM"),
    Track("g3", "Creep", ("Radiohead",), "Pablo Honey"),
]


@pytest.fixture
def library(repository):
    repository.upsert_tracks(LIBRARY)
    return repository


def prepared(library, config, size=None):
    packet, _remaining = prepare(library, config, size=size)
    return packet


def respond(packet, *titles_and_moods):
    items = [item(n, title, mood=mood) for n, (title, mood) in enumerate(titles_and_moods, 1)]
    return answer(*items, paquet=packet.id)


def test_a_packet_is_written_down_so_the_answer_can_be_read_later(library, config):
    packet = prepared(library, config)
    assert load_packet(config.claude.packet_file).id == packet.id


def test_the_packet_size_is_respected(library, config):
    assert len(prepared(library, config, size=2).subjects) == 2


def test_an_import_writes_the_file_and_sorts_the_library(library, config):
    packet = prepared(library, config)
    titles = [(s.title, "Calme") for s in packet.subjects]
    result = import_answer(respond(packet, *titles), library, config, TAXONOMY)

    assert result.accepted == 3
    assert result.applied == 3
    assert result.remaining == 0
    assert len(verdicts.load(config.claude.verdicts_file)) == 3
    assert all(c.judged and c.mood == "Calme" for c in library.classifications())


def test_judged_titles_leave_the_next_packet(library, config):
    """Ce qui a été jugé ne doit pas être recollé : c'est le principe même."""
    packet = prepared(library, config, size=2)
    import_answer(respond(packet, *[(s.title, "Calme") for s in packet.subjects]),
                  library, config, TAXONOMY)

    following = prepared(library, config)
    judged = {s.title for s in packet.subjects}
    assert [s.title for s in following.subjects] == [
        t.title for t in LIBRARY if t.title not in judged
    ]


def test_the_rest_of_an_interrupted_answer_can_be_imported_after(library, config):
    """Claude s'est arrêté au premier titre ; la suite, obtenue par
    « continue », se colle à son tour sur le même paquet."""
    packet = prepared(library, config)
    first = import_answer(respond(packet, (packet.subjects[0].title, "Calme")),
                          library, config, TAXONOMY)
    assert first.left_in_packet == 2

    rest = answer(item(2, packet.subjects[1].title), item(3, packet.subjects[2].title),
                  paquet=packet.id)
    second = import_answer(rest, library, config, TAXONOMY)
    assert second.left_in_packet == 0 and second.remaining == 0


def test_a_hand_corrected_line_survives_an_import(library, config):
    packet = prepared(library, config)
    mine = verdicts.manual(Verdict("Radiohead", "Creep", "Rock", "Grunge", "Sombre", 1.0))
    verdicts.save(VerdictBook([mine]), config.claude.verdicts_file)

    import_answer(respond(packet, *[(s.title, "Calme") for s in packet.subjects]),
                  library, config, TAXONOMY)
    kept = verdicts.load(config.claude.verdicts_file).get(mine.key)
    assert (kept.mood, kept.source) == ("Sombre", "manuel")


def test_importing_without_a_packet_says_what_to_do(library, config):
    with pytest.raises(ChatError, match="prépare"):
        import_answer(answer(*GOOD), library, config, TAXONOMY)


def test_a_fully_judged_library_has_no_packet(library, config):
    verdicts.save(
        VerdictBook([Verdict(t.artist, t.title, "Rock", "Grunge", "Calme", 0.9) for t in LIBRARY]),
        config.claude.verdicts_file,
    )
    assert prepare(library, config) == (None, 0)
