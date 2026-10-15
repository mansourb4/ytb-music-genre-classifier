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

    def send(self, chat, text, rows=None, reply_to=None):
        self.sent.append((chat, text, rows))
        self.replies = getattr(self, "replies", []) + [reply_to]
        return 100 + len(self.sent)

    def send_audio(self, chat, path, title="", performer=""):
        self.audio = getattr(self, "audio", []) + [(path.read_bytes(), title, performer)]
        return 900

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


def test_a_message_telegram_did_not_take_is_sent_again_on_the_next_turn(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    world.liked.insert(0, KOHNDO)
    sent = world.bot.send

    def down(chat, text, rows=None, reply_to=None):
        raise TelegramError("sendMessage : Telegram injoignable (timed out)")

    world.bot.send = down
    with pytest.raises(TelegramError):
        _run(tmp_path, world)
    # Le titre est retenu, son message pas encore parti ; aucun nouveau like ensuite.
    world.bot.send = sent
    services = world.services()
    veille.run(tmp_path, services, interval=10_000, once=True)
    assert len(world.bot.sent) == 1 and "Kohndo" in world.bot.sent[0][1]
    assert next(iter(nouveautes.load(tmp_path).pending.values())).message_id == 101


def test_restart_waits_for_launchd_to_let_go_of_the_old_veille(monkeypatch, tmp_path):
    import subprocess

    from ytmgc import cli

    calls, state = [], {"loaded": 3, "refusals": 1}

    def launchctl(*arguments):
        calls.append(arguments[0])
        if arguments[0] == "print":
            state["loaded"] -= 1
            return subprocess.CompletedProcess(arguments, 0 if state["loaded"] > 0 else 113)
        if arguments[0] == "bootstrap" and state["refusals"]:
            state["refusals"] -= 1
            return subprocess.CompletedProcess(arguments, 5, "", "Bootstrap failed: 5: Input/output error")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(cli, "_launchctl", launchctl)
    assert cli._veille_restart("gui/501", tmp_path / "x.plist", sleep=lambda _: None) is None
    assert calls == ["bootout", "print", "print", "print", "bootstrap", "bootstrap"]

    state.update(loaded=0, refusals=99)
    error = cli._veille_restart("gui/501", tmp_path / "x.plist", sleep=lambda _: None, attempts=2)
    assert error == "Bootstrap failed: 5: Input/output error"


def test_a_title_the_rules_leave_out_still_gets_a_proposal():
    plans = {"Jazz · Bop & cool nocturne": ["m1", "m2"], "Jazz · Modal": ["m3", "x"],
             "Rap · FR posé": ["r1"]}
    tracks = {"m1": Track("m1", "So What", ("Miles Davis",)),
              "m2": Track("m2", "Blue in Green", ("Miles Davis", "Bill Evans")),
              "m3": Track("m3", "Naima", ("John Coltrane",)),
              "x": Track("x", "Impressions", ("John Coltrane",)),
              "r1": Track("r1", "Rap", ("Kohndo",))}
    classes = {"m3": Classification("m3", MatchStatus.MATCHED, genres=("Jazz",), styles=("Modal",)),
               "x": Classification("x", MatchStatus.MATCHED, genres=("Jazz",), styles=("Modal",)),
               "r1": Classification("r1", MatchStatus.MATCHED, genres=("Hip Hop",))}
    miles = Track("new", "It Never Entered My Mind", ("The Miles Davis Quintet",))
    assert nouveautes.guess(miles, None, plans, tracks, classes) == (
        "Jazz · Bop & cool nocturne", "là où sont ses autres titres")

    stranger = Track("s", "Inconnu", ("Personne",))
    modal = Classification("s", MatchStatus.MATCHED, genres=("Jazz",), styles=("Modal", "Hard Bop"))
    assert nouveautes.guess(stranger, modal, plans, tracks, classes) == ("Jazz · Modal",
                                                                       "styles proches")
    rap = Classification("s", MatchStatus.MATCHED, genres=("Hip Hop",), styles=("Trap",))
    assert nouveautes.guess(stranger, rap, plans, tracks, classes) == ("Rap · FR posé", "même genre")
    assert nouveautes.guess(stranger, None, plans, tracks, classes) == (None, None)


def test_a_waiting_title_without_proposal_gets_one_and_its_message_is_updated(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    lonely = Track("m9", "Lonely", ("Moderat",))
    state = nouveautes.load(tmp_path)
    state.add(nouveautes.Pending("m9", "Moderat", "Lonely", "texte", None, 55))
    nouveautes.save(tmp_path, state)
    world.db["m9"] = lonely
    _run(tmp_path, world)
    message_id, text, rows = world.bot.edits[-1]
    assert message_id == 55 and "Nouvelle proposition plus bas" in text and rows is None
    _, text, rows = world.bot.sent[-1]
    assert "Proposé : <b>Jazz · Modal</b> (là où sont ses autres titres)" in text
    assert rows[0][0][0] == "✅ Jazz · Modal"
    assert next(iter(nouveautes.load(tmp_path).pending.values())).message_id == 100 + len(
        world.bot.sent)


def test_same_artist():
    assert nouveautes.same_artist("The Miles Davis Quintet", "Miles Davis")
    assert nouveautes.same_artist("Miles Davis", "miles  DAVIS")
    assert nouveautes.same_artist("Damso", "damso")
    assert not nouveautes.same_artist("Damso", "Damso Fan Club")
    assert not nouveautes.same_artist("Miles Davis", "Miles Kane")
    assert not nouveautes.same_artist("", "Miles Davis")


def _say(update_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": int(CHAT)}, "text": text}}


def test_proposer_brings_back_a_title_left_out(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    world.liked.insert(0, KOHNDO)
    _run(tmp_path, world)
    key = next(iter(nouveautes.load(tmp_path).pending))
    world.bot.queue = [_tap(1, f"x:{key}")]
    _run(tmp_path, world)
    assert "/proposer Faut qu'je tienne" in world.bot.edits[-1][1]
    assert nouveautes.load(tmp_path).pending == {}

    world.bot.queue = [_say(2, "/proposer  KOHNDO ")]
    _run(tmp_path, world)
    _, text, rows = world.bot.sent[-1]
    assert "Proposé : <b>Rap · FR posé</b>" in text and rows[0][0][0] == "✅ Rap · FR posé"
    key = next(iter(nouveautes.load(tmp_path).pending))
    world.bot.queue = [_tap(3, f"c:{key}:{nouveautes.load(tmp_path).names.index('Rap · FR posé')}")]
    _run(tmp_path, world)
    placed = placements.load(world.placements_file).get(placements.entry_key(
        "Kohndo", "Faut qu'je tienne"))
    assert placed.playlist == "Rap · FR posé" and "k1" in world.apple

    world.bot.queue = [_say(4, "/proposer inconnu"), _say(5, "/proposer")]
    _run(tmp_path, world)
    assert "ne contient « inconnu »" in world.bot.sent[-2][1]
    assert world.bot.sent[-1][1].startswith("Écris par exemple")


def test_ignorer_sets_a_proposal_aside_until_it_is_called_back(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    world.liked.insert(0, KOHNDO)
    _run(tmp_path, world)
    rows = world.bot.sent[-1][2]
    assert rows[-1] == [("⏸ Ignorer pour l'instant", "i:1")]

    world.bot.queue = [_tap(1, "i:1")]
    _run(tmp_path, world)
    assert "⏸ Ignoré pour l'instant" in world.bot.edits[-1][1] and world.runs == []
    state = nouveautes.load(tmp_path)
    assert state.pending == {} and state.ignored == {"k1": "Kohndo – Faut qu'je tienne"}
    # Toujours tenu hors playlist : rien ne se range ni ne se télécharge.
    held = placements.load(world.placements_file).get(placements.entry_key(
        "Kohndo", "Faut qu'je tienne"))
    assert held.nowhere

    world.bot.queue = [_say(2, "/ignores")]
    _run(tmp_path, world)
    assert "• Kohndo – Faut qu'je tienne" in world.bot.sent[-1][1]

    world.bot.queue = [_say(3, "/proposer kohndo")]
    _run(tmp_path, world)
    assert "Proposé : <b>Rap · FR posé</b>" in world.bot.sent[-1][1]
    assert nouveautes.load(tmp_path).ignored == {}


class _Stop(BaseException):
    pass


def test_verifier_looks_at_the_likes_right_away(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)  # premier passage : les likes existants
    calls = []

    def updates(offset, wait=0):
        calls.append(wait)
        if len(calls) == 1:
            # Pendant l'écoute : un like arrive, puis tu demandes « /verifier ».
            world.liked.insert(0, KOHNDO)
            return [_say(1, "/verifier")]
        raise RuntimeError("fin de l'essai")

    def sleep(seconds):
        raise _Stop()

    world.bot.updates = updates
    with pytest.raises(_Stop):
        # Relecture toutes les heures : seule la demande peut la déclencher.
        veille.run(tmp_path, world.services(), interval=3600, clock=lambda: 0.0, sleep=sleep)
    texts = [text for _, text, _ in world.bot.sent]
    assert texts[0] == "🔎 Je regarde tes likes…"
    assert texts[1] == ("1 nouveau(x) like(s) :\n"
                        "• Kohndo – Faut qu'je tienne : proposition ci-dessous.")
    assert "Kohndo" in texts[2]


def test_each_proposal_comes_with_an_audio_clip(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    clips = []

    def preview(pending):
        clips.append((pending.video_id, pending.duration))
        path = tmp_path / "clip.m4a"
        path.write_bytes(b"son")
        return path

    services = world.services()
    services.preview = preview
    world.liked.insert(0, Track("k1", "Faut qu'je tienne", ("Kohndo",), duration_s=200))
    veille.run(tmp_path, services, interval=0, once=True)
    assert clips == [("k1", 200)]
    assert world.bot.audio == [(b"son", "Faut qu'je tienne", "Kohndo")]
    assert world.bot.replies[-1] == 900  # la proposition répond à l'extrait
    assert not (tmp_path / "clip.m4a").exists()  # le fichier temporaire est effacé
    veille.run(tmp_path, services, interval=0, once=True)
    assert len(clips) == 1  # un seul extrait par titre


def test_a_failed_clip_never_holds_back_the_proposal(tmp_path):
    world = World(tmp_path, [Track("old", "Rain", ("Moderat",))])
    _run(tmp_path, world)
    services = world.services()
    services.preview = lambda pending: (_ for _ in ()).throw(RuntimeError("yt-dlp a échoué"))
    world.liked.insert(0, KOHNDO)
    veille.run(tmp_path, services, interval=0, once=True)
    assert "Kohndo" in world.bot.sent[-1][1] and world.bot.replies[-1] is None


def test_preview_window():
    assert nouveautes.preview_window(None) == (45, 75)
    assert nouveautes.preview_window(30) == (0, 30)
    assert nouveautes.preview_window(200) == (70, 100)
    assert nouveautes.preview_window(50) == (17, 47)


def test_bot_uploads_an_audio_file(tmp_path):
    sent = []

    def opener(request, timeout):
        sent.append((request.full_url, request.headers, request.data))
        return _Response(json.dumps({"ok": True, "result": {"message_id": 7}}).encode())

    clip = tmp_path / "x.m4a"
    clip.write_bytes(b"AUDIO")
    assert Bot("SECRET", opener=opener).send_audio(CHAT, clip, title="Rain",
                                                  performer="Moderat") == 7
    url, headers, body = sent[0]
    assert url.endswith("/sendAudio") and b"AUDIO" in body
    assert b'name="title"' in body and b"Moderat" in body
    assert headers["Content-type"].startswith("multipart/form-data; boundary=")


def test_verifier_says_what_became_of_a_like_already_placed(tmp_path):
    world = World(tmp_path, [Track("x", "Autre", ("Quelqu'un",))])
    _run(tmp_path, world)
    held = Track("h", "Tenu", ("Personne",))
    book = placements.load(world.placements_file)
    book.put(held, nouveautes.NOWHERE)
    placements.save(book, world.placements_file)
    world.liked[:0] = [Track("old", "Rain", ("Moderat",)), held]
    state = nouveautes.load(tmp_path)
    veille.check(state, world.services())
    assert state.last_report == [
        "• Moderat – Rain : déjà dans <b>Jazz · Modal</b>, mis en favori.",
        "• Personne – Tenu : tenu hors playlists (« (aucune) »), mis en favori. "
        "Pour le ranger : /proposer Tenu",
    ]
    assert world.bot.sent == []
