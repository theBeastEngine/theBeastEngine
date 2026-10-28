"""/export, /result and the idle-league credit saver (9-10 Oct 2026)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import services.telegram_bot as tb
from config.settings import PINNACLE
from core.pinref import PinRef


def _p(i, **kw):
    p = dict(id=i, created_at=datetime(2026, 10, 9, 8, i % 60), mode="prematch", league="euroleague",
             home="Real Madrid", away="Maccabi, Tel Aviv", market="spreads", selection="home", line=-4.5,
             book="sportybet", book_odds=1.95, took_odds=None, fair_odds=1.8, min_odds=1.87, ev=0.052,
             stake=5000, status="new", settled_at=None)
    p.update(kw)
    return p


class FakeRepo:
    def __init__(self, picks=None, pick=None):
        self.picks, self.pick, self.settled = picks or [], pick, []

    def all_picks(self, limit=200):
        return self.picks[:limit]

    def get_pick(self, pid):
        return self.pick

    def settle_pick(self, pid, res):
        self.settled.append((pid, res))
        return 105000.0


def _bot(monkeypatch, repo):
    monkeypatch.setattr(tb, "repo", repo)
    return tb.TelegramBot(SimpleNamespace(paused=False))


# ---------------- export ----------------
def test_export_chunks_are_csv_in_time_order_and_quote_commas():
    chunks = tb.export_chunks([_p(2), _p(1)])
    assert len(chunks) == 1
    lines = chunks[0].split("\n")
    assert lines[0].startswith("id,time_utc,mode,league,game,bet")
    assert lines[1].startswith("1,") and lines[2].startswith("2,")          # oldest first
    assert '"Real Madrid vs Maccabi, Tel Aviv"' in lines[1]                  # a comma in a name is quoted
    assert "5.2" in lines[1] and "2026-10-09 08:01" in lines[1]


def test_export_chunks_split_on_line_boundaries_and_repeat_the_header():
    chunks = tb.export_chunks([_p(i) for i in range(1, 301)])
    assert len(chunks) > 1
    ids = []
    for c in chunks:
        assert len(c) <= tb.EXPORT_CHUNK + 5
        ls = c.split("\n")
        assert ls[0].startswith("id,time_utc")
        ids += [int(x.split(",")[0]) for x in ls[1:]]
    assert ids == list(range(1, 301))                                        # nothing lost, nothing repeated


def test_export_command_sends_escaped_pre_blocks(monkeypatch):
    sent = []
    monkeypatch.setattr(tb.tn, "send", lambda text, *a, **k: sent.append(text) or True)
    bot = _bot(monkeypatch, FakeRepo([_p(1, home="A&B <x>")]))
    out = bot.handle_text("/export")
    assert sent and sent[0].startswith("<pre>") and sent[0].endswith("</pre>")
    assert "A&amp;B &lt;x&gt;" in sent[0] and "<x>" not in sent[0]
    assert "Exported 1 picks" in out and "before any deploy" in out.lower()


def test_export_command_handles_empty_bad_arg_and_failed_send(monkeypatch):
    assert "No picks" in _bot(monkeypatch, FakeRepo([])).handle_text("/export")
    assert "Usage" in _bot(monkeypatch, FakeRepo([_p(1)])).handle_text("/export many")
    monkeypatch.setattr(tb.tn, "send", lambda *a, **k: False)
    assert "failed to send" in _bot(monkeypatch, FakeRepo([_p(1)])).handle_text("/export")


# ---------------- result ----------------
def test_result_settles_only_placed_picks(monkeypatch):
    repo = FakeRepo(pick=_p(7, status="placed"))
    out = _bot(monkeypatch, repo).handle_text("/result 7 won")
    assert repo.settled == [(7, "won")] and "105,000" in out


def test_result_refuses_unplaced_settled_unknown_and_bad_input(monkeypatch):
    for status, word in (("new", "not placed"), ("skipped", "not placed"), ("won", "already")):
        repo = FakeRepo(pick=_p(7, status=status))
        assert word in _bot(monkeypatch, repo).handle_text("/result 7 lost")
        assert repo.settled == []
    assert "No pick #9" in _bot(monkeypatch, FakeRepo(pick=None)).handle_text("/result 9 won")
    for bad in ("/result", "/result 7", "/result x won", "/result 7 maybe", "/result 7 won extra"):
        assert "Usage" in _bot(monkeypatch, FakeRepo(pick=_p(7, status="placed"))).handle_text(bad)


def test_help_lists_the_new_commands():
    assert "/result" in tb.HELP and "/export" in tb.HELP


# ---------------- idle-league credit saver ----------------
def _raw(gid, h, a, hours):
    t = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": gid, "home_team": h, "away_team": a, "commence_time": t, "bookmakers": [
        {"key": PINNACLE, "markets": [
            {"key": "totals", "outcomes": [{"name": "Over", "price": 1.90, "point": 220.5},
                                           {"name": "Under", "price": 1.92, "point": 220.5}]}]}]}


class CountingAPI:
    def __init__(self, raws):
        self.raws, self.calls = raws, 0

    def fetch(self, key, markets=None, books=None, regions="eu"):
        self.calls += 1
        return self.raws


def _ref(tmp_path, raws, hours_old, game_hours):
    api = CountingAPI(raws)
    ref = PinRef(api=api, path=str(tmp_path / "p.json"), daily_cap=100)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ref.data["leagues"]["nba"] = {
        "fetched_at": (now - timedelta(hours=hours_old)).isoformat(),
        "events": [] if game_hours is None else [{"event_id": "g", "home": "A", "away": "B", "markets": {},
                                                  "commence": (now + timedelta(hours=game_hours)).isoformat()}]}
    return ref, api


def test_league_with_a_game_this_week_still_refreshes_every_4_hours(tmp_path):
    ref, api = _ref(tmp_path, [_raw("g", "A", "B", 30)], hours_old=5, game_hours=30)
    assert ref.ensure_fresh("nba") and api.calls == 1


def test_idle_league_is_not_refetched_after_4_hours(tmp_path):
    for game_hours in (None, 24 * 11):          # off-season (no games) and opening night 11 days away
        ref, api = _ref(tmp_path, [_raw("g", "A", "B", 30)], hours_old=5, game_hours=game_hours)
        assert ref.ensure_fresh("nba") and api.calls == 0


def test_idle_league_is_refetched_after_a_day(tmp_path):
    ref, api = _ref(tmp_path, [_raw("g", "A", "B", 30)], hours_old=25, game_hours=24 * 11)
    ref.ensure_fresh("nba")
    assert api.calls == 1


def test_idle_saver_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("PIN_REF_IDLE_MAX_AGE_MIN", "240")
    ref, api = _ref(tmp_path, [_raw("g", "A", "B", 30)], hours_old=5, game_hours=None)
    ref.ensure_fresh("nba")
    assert api.calls == 1
