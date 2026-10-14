"""`ytmgc apple-music-likes` : identifiants Apple Music périmés."""

from ytmgc import apple_music, cli
from ytmgc.config import Config
from ytmgc.models import Track
from ytmgc.store import Repository, connect


class FakeClient:
    def playlist_entries(self, playlist_id):
        assert playlist_id == "LM"
        return [{"video_id": "dup", "label": "Moderat – Rain", "title": "Rain",
                 "artists": ["Moderat"], "liked": True}]


def test_a_stale_id_is_forgotten_and_the_title_found_again(tmp_path, monkeypatch, capsys):
    # Deux versions du même morceau : « dup » est resté sur un identifiant
    # Music disparu, « main » porte le bon.
    repository = Repository(connect(":memory:"))
    repository.upsert_tracks([Track("main", "Rain", ("Moderat",)),
                              Track("dup", "Rain", ("Moderat",))])
    apple_music.save_state(tmp_path, apple_music.State({"main": "LIVE", "dup": "OLD"}))
    monkeypatch.setattr(cli, "_youtube", lambda _config: FakeClient())
    monkeypatch.setattr(cli, "_repository", lambda _config: repository)
    runs = []

    def run_favorites(pids, **kwargs):
        runs.append(list(pids))
        if pids == ["OLD"]:
            return apple_music.FavoritesResult(gone=1, gone_pids=["OLD"])
        return apple_music.FavoritesResult(already=len(pids))

    monkeypatch.setattr(apple_music, "run_favorites", run_favorites)
    args = cli.build_parser().parse_args(["apple-music-likes", "--dossier", str(tmp_path)])
    assert args.func(args, Config()) == 0

    assert runs == [["OLD"], ["LIVE"]]
    assert apple_music.load_state(tmp_path).known == {"main": "LIVE"}
    out = capsys.readouterr().out
    assert "1 identifiant(s) Apple Music périmé(s) oublié(s) ; 1 titre(s) retrouvé(s)" in out
    assert "relance « ytmgc apple-music »" not in out
