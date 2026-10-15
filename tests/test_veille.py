"""Veille des likes : proposer sur Telegram, retenir la réponse, ranger."""

import io
import json

import pytest

from ytmgc import nouveautes, placements, veille
from ytmgc.models import Classification, MatchStatus, Track
from ytmgc.telegram import Bot, TelegramError

CHAT = "42"


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.answers = []
        self.queue = []

    def send(self, chat, text, rows=None):
        self.sent.append((chat, text, rows))
        return 100 + len(self.sent)

    def edit(self, chat, message_id, text, rows=None):
        self.edits.append((message_id, text, rows))

    def answer(self, callback_id, text=""):
        self.answers.append((callback_id, text))

    def updates(self, offset, wait=0):
        queued, self.queue = self.queue, []
        return queued


def _tap(update_id, data, chat=CHAT):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data, "message": {"chat": {"id": int(chat)}}}}


class World:
    """YouTube, base et chaîne, en mémoire."""

    def __init__(self, tmp_path, liked):
        self.liked = list(liked)
        self.db = {"old": Track("old", "Rain", ("Moderat",))}
        self.classes = {}
        self.plan = {"Jazz · Modal": ["old"], "Jazz · Fusion planante": [],
                     "Rap · FR posé": [], "Rap · FR groovy": []}
        self.placements_file = tmp_path / "placements.txt"
        self.apple = set()
        self.runs = []
        self.bot = FakeBot()

    def plans(self):
        book = placements.load(self.placements_file)
        found = {name: list(ids) for name, ids in self.plan.items()}
        for video_id, track in self.db.items():
            placed = book.get(placements.entry_key(track.artist, track.title))
            if placed is not None:
                for ids in found.values():
                    if video_id in ids:
                        ids.remove(video_id)
                if not placed.nowhere:
                    found[placed.playlist].append(video_id)
            elif video_id in self.classes and not any(video_id in i for i in found.values()):
                found[self.classes[video_id]].append(video_id)
        return found

    def classify(self, tracks):
        for track in tracks:
            if track.artist == "Kohndo":
                self.classes[track.video_id] = "Rap · FR posé"

    def classifications(self):
        return [Classification(v, MatchStatus.MATCHED, genres=("Hip Hop",), styles=("Boom Bap",),
                               year=2005) for v in self.classes]

    def pipeline(self, full):
        self.runs.append(full)
        if full:
            book = placements.load(self.placements_file)
            for video_id, track in self.db.items():
                placed = book.get(placements.entry_key(track.artist, track.title))
                if placed is not None and not placed.nowhere:
                    self.apple.add(video_id)
        return []

    def services(self):
        return veille.Services(
            liked=lambda limit: self.liked[:limit] if limit else list(self.liked),
            tracks=lambda: list(self.db.values()),
            save_tracks=lambda tracks: self.db.update({t.video_id: t for t in tracks}),
            classify=self.classify,
            classifications=self.classifications,
            plans=self.plans,
            placements_file=self.placements_file,
            pipeline=self.pipeline,
            in_apple=lambda video_id: video_id in self.apple,
            bot=self.bot,
            chat=CHAT,
            log=lambda line: None,
        )


def _run(tmp_path, world):
    veille.run(tmp_path, world.services(), interval=0, once=True)


KOHNDO = Track("k1", "Faut qu'je tienne", ("Kohndo",), album="Tout est écrit")


def test_the_first_pass_only_remembers_what_is_already_liked(tmp_path):
    world = World(tmp_path, [world_track := Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    assert world.bot.sent == [] and world.runs == []
    assert nouveautes.load(tmp_path).seen == {world_track.video_id}


def test_a_new_like_is_proposed_held_then_placed_where_you_choose(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    world.liked.insert(0, KOHNDO)
    _run(tmp_path, world)

    (chat, text, rows), = world.bot.sent
    assert "Kohndo – Faut qu'je tienne" in text and "Hip Hop · Boom Bap (2005)" in text
    assert "Proposé : <b>Rap · FR posé</b>" in text
    labels = [label for row in rows for label, _ in row]
    assert labels[:2] == ["✅ Rap · FR posé", "Rap · FR groovy"]
    assert "Autre famille…" in labels and "Ne pas ranger" in labels
    # Tenu hors playlist en attendant : la chaîne ne le range pas sans toi.
    held = placements.load(world.placements_file).get(placements.entry_key("Kohndo",
                                                                          "Faut qu'je tienne"))
    assert held.nowhere and world.runs == []

    # Autre famille… → Jazz → Fusion planante.
    state = nouveautes.load(tmp_path)
    key = next(iter(state.pending))
    world.bot.queue = [_tap(1, f"f:{key}")]
    _run(tmp_path, world)
    families = [label for row in world.bot.edits[-1][2] for label, _ in row]
    assert families[:2] == ["Jazz", "Rap"]
    world.bot.queue = [_tap(2, f"g:{key}:0")]
    _run(tmp_path, world)
    choice = next(data for row in world.bot.edits[-1][2] for label, data in row
                  if label == "Fusion planante")
    world.bot.queue = [_tap(3, choice)]
    _run(tmp_path, world)

    placed = placements.load(world.placements_file).get(placements.entry_key(
        "Kohndo", "Faut qu'je tienne"))
    assert placed.playlist == "Jazz · Fusion planante"
    assert world.runs == [True] and "k1" in world.apple
    assert "✅ Dans <b>Jazz · Fusion planante</b>" in world.bot.edits[-1][1]
    assert world.bot.edits[-1][2] is None  # plus de boutons
    assert nouveautes.load(tmp_path).pending == {}


def test_ne_pas_ranger_keeps_it_out_and_downloads_nothing(tmp_path):
    world = World(tmp_path, [])
    world.liked = [Track("old", "Rain", ("Moderat",))]
    _run(tmp_path, world)
    world.liked.insert(0, KOHNDO)
    _run(tmp_path, world)
    key = next(iter(nouveautes.load(tmp_path).pending))
    world.bot.queue = [_tap(1, f"x:{key}")]
    _run(tmp_path, world)
    assert world.runs == [] and "Laissé hors playlists" in world.bot.edits[-1][1]
    assert nouveautes.load(tmp_path).pending == {}


def test_a_like_already_placed_is_only_made_a_favourite(tmp_path):
    world = World(tmp_path, [Track("x", "Autre", ("Quelqu'un",))])
    _run(tmp_path, world)
    world.liked.insert(0, Track("old", "Rain", ("Moderat",)))
    _run(tmp_path, world)
    assert world.bot.sent == [] and world.runs == [False]


def test_taps_from_another_chat_are_ignored(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    world.liked.insert(0, KOHNDO)
    _run(tmp_path, world)
    key = next(iter(nouveautes.load(tmp_path).pending))
    world.bot.queue = [_tap(1, f"x:{key}", chat="666")]
    _run(tmp_path, world)
    assert nouveautes.load(tmp_path).pending and world.bot.answers == [("cb1", "")]


def test_a_title_that_never_arrives_is_retried_then_given_up(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    world.liked.insert(0, KOHNDO)
    _run(tmp_path, world)
    key = next(iter(nouveautes.load(tmp_path).pending))
    world.pipeline = lambda full: world.runs.append(full) or ["« telecharge » en échec"]
    world.bot.queue = [_tap(1, f"c:{key}:0")]
    _run(tmp_path, world)
    assert "⚠️ Pas encore dans Apple Music (« telecharge » en échec)" in world.bot.edits[-1][1]
    for _ in range(veille.MAX_ATTEMPTS - 1):
        _run(tmp_path, world)
    assert "❌" in world.bot.edits[-1][1]
    assert nouveautes.load(tmp_path).pending == {}
    assert len(world.runs) == veille.MAX_ATTEMPTS


def test_a_message_gets_the_status(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    world.bot.queue = [{"update_id": 9, "message": {"chat": {"id": 42}, "text": "/etat"}}]
    _run(tmp_path, world)
    assert world.bot.sent[-1][1].startswith("Veille active. 0 titre(s)")


def test_state_round_trip(tmp_path):
    state = nouveautes.State(seen={"a"}, offset=7, names=["Jazz · Modal"])
    key = state.add(nouveautes.Pending("k1", "Kohndo", "Titre", "texte", "Jazz · Modal", 101))
    nouveautes.save(tmp_path, state)
    again = nouveautes.load(tmp_path)
    assert again.pending[key] == state.pending[key]
    assert (again.seen, again.offset, again.names, again.next_id) == ({"a"}, 7, ["Jazz · Modal"], 2)


def test_stale_or_unknown_buttons_do_nothing():
    state = nouveautes.State(names=["Jazz · Modal"])
    key = state.add(nouveautes.Pending("k1", "A", "T", "texte", "Jazz · Modal", 1))
    assert nouveautes.interpret(state, "c:99:0") is None
    assert nouveautes.interpret(state, f"c:{key}:7") is None
    assert nouveautes.interpret(state, "zz") is None
    assert nouveautes.interpret(state, f"c:{key}:0").decision == "Jazz · Modal"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_bot_sends_buttons_and_hides_its_token_on_errors():
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, json.loads(request.data), timeout))
        return _Response(json.dumps({"ok": True, "result": {"message_id": 5}}).encode())

    bot = Bot("SECRET", opener=opener)
    assert bot.send(CHAT, "Salut", [[("A", "c:1:0")]]) == 5
    url, body, _ = calls[0]
    assert url.endswith("/sendMessage")
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "A", "callback_data": "c:1:0"}]]}

    import urllib.error

    def failing(request, timeout):
        raise urllib.error.URLError("hors ligne")

    with pytest.raises(TelegramError) as caught:
        Bot("SECRET", opener=failing).send(CHAT, "x")
    assert "SECRET" not in str(caught.value)


def test_playlist_names_are_escaped_for_telegram():
    text = nouveautes.describe(Track("v", "Kiss & Tell", ("A",)), None, "Soul · R&B posés")
    assert "Kiss &amp; Tell" in text and "Soul · R&amp;B posés" in text


def test_etat_explains_what_became_of_the_last_likes(tmp_path, monkeypatch, capsys):
    from ytmgc import cli
    from ytmgc.config import Config
    from ytmgc.store import Repository, connect

    repository = Repository(connect(":memory:"))
    monkeypatch.setattr(cli, "_repository", lambda _config: repository)
    monkeypatch.setattr(cli, "_plan_map", lambda _repo, _config: {"Jazz · Modal": ("old",)})
    monkeypatch.setattr(cli, "_veille_paths", lambda: (tmp_path / "absent.plist",
                                                       tmp_path / "absent.log"))

    class YouTube:
        def liked_tracks(self, limit):
            return [KOHNDO, Track("old", "Rain", ("Moderat",)), Track("x", "Avant", ("Q",))]

    monkeypatch.setattr(cli, "_youtube", lambda _config: YouTube())
    nouveautes.save(tmp_path, nouveautes.State(seen={"old", "x"}, checked_at="2026-10-15 10:00"))
    config = Config()
    config.taxonomy.placements_file = str(tmp_path / "placements.txt")

    args = cli.build_parser().parse_args(["veille", "--etat", "--dossier", str(tmp_path)])
    assert args.func(args, config) == 0
    out = capsys.readouterr().out
    assert "Pas installée en fond" in out and "JETON ABSENT" in out
    assert "Dernière relecture : 2026-10-15 10:00" in out
    assert "Faut qu'je tienne — pas encore relu" in out
    assert "Rain — déjà rangé" in out
    assert "Avant — liké avant le premier passage" in out
