from datetime import datetime, timedelta, timezone

from core import grader as G


class FakeRepo:
    def __init__(self, picks):
        self.picks = picks
        self.settled = []

    def all_picks(self, n=0):
        return self.picks

    def settle_pick(self, pid, res):
        self.settled.append((pid, res))
        for p in self.picks:
            if p["id"] == pid:
                p["status"] = res
        return 100500.0


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc).timestamp()


def _pick(i, market, sel, line, status="new", mode="prematch", lg="nbl", odds=1.9, fair=1.75):
    tip = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)      # long finished
    return dict(id=i, league=lg, home="Melbourne United", away="Illawarra Hawks", commence=tip, market=market,
                selection=sel, line=line, status=status, mode=mode, book_odds=odds, took_odds=None, fair_odds=fair, stake=5000)


def _score(h=95, a=88, done=True):
    return [dict(id="x", commence_time="2026-10-18T00:30:00Z", home_team="Melbourne United", away_team="Illawarra Hawks",
                 completed=done, scores=[dict(name="Melbourne United", score=str(h)), dict(name="Illawarra Hawks", score=str(a))])]


def test_final_score_reads_completed_games_only():
    assert G.final_score(_score()[0]) == (95, 88)
    assert G.final_score(_score(done=False)[0]) is None


def test_grade_pick_full_game_markets_only():
    assert G.grade_pick(_pick(1, "totals", "over", 180.5), 95, 88) == "won"
    assert G.grade_pick(_pick(1, "totals", "under", 183.0), 95, 88) == "void"
    assert G.grade_pick(_pick(1, "spreads", "away", -10.5), 95, 88) == "won"       # away +10.5 loses by 7: covers
    assert G.grade_pick(_pick(1, "totals_q1", "over", 40.5), 95, 88) is None      # quarter market: needs /score


def test_tick_grades_all_and_settles_only_placed():
    picks = [_pick(1, "totals", "over", 180.5, status="placed"),     # 183 total -> won, placed: bankroll moves
             _pick(2, "spreads", "home", -10.5),                      # margin 7 -> lost, not placed: background only
             _pick(3, "h2h", "home", None),                           # home wins -> won
             _pick(4, "totals_q1", "over", 40.5)]                    # quarter market: cannot be graded here
    store, sent = {}, []
    g = G.Grader(None, load=lambda: store.get("v"), save=lambda v: store.__setitem__("v", v), now=lambda: NOW,
                 repo=FakeRepo(picks), send=sent.append, scores=lambda lg, days: _score())
    assert g.tick() == 3
    assert g.grades["1"]["res"] == "won" and g.grades["2"]["res"] == "lost" and g.grades["3"]["res"] == "won"
    assert "4" not in g.grades
    assert g._repo().settled == [(1, "won")] and len(sent) == 1 and "BET SETTLED (auto)" in sent[0]
    assert g.tick() == 0                                              # nothing is graded twice


def test_tick_skips_unfinished_and_recent_games():
    picks = [_pick(1, "totals", "over", 180.5)]
    g = G.Grader(None, load=lambda: None, save=lambda v: None, now=lambda: NOW, repo=FakeRepo(picks),
                 scores=lambda lg, days: _score(done=False))
    assert g.tick() == 0
    young = [_pick(2, "totals", "over", 180.5)]
    young[0]["commence"] = datetime.fromtimestamp(NOW - 3600, tz=timezone.utc)
    assert G.Grader(None, now=lambda: NOW, repo=FakeRepo(young), scores=lambda lg, d: _score()).tick() == 0


def test_winrate_rows_counts_graded_picks_with_roi():
    picks = [_pick(1, "totals", "over", 180.5, odds=2.0, fair=1.8), _pick(2, "totals", "under", 180.5, odds=2.0, fair=1.8),
             _pick(3, "h2h", "home", None, mode="live", odds=1.5, fair=1.4, status="lost"),
             _pick(4, "h2h", "home", None, odds=1.5, fair=1.4)]
    grades = {"1": dict(res="won"), "2": dict(res="lost")}
    rows = {r[0]: r for r in G.winrate_rows(picks, grades)}
    assert rows["All picks"][1:3] == (3, 1) and abs(rows["All picks"][5] - (1.0 - 1.0 - 1.0) / 3 * 100) < 1e-6
    assert rows["Prematch"][1] == 2 and rows["Live"][1] == 1


def test_winrate_command_reads_grades(monkeypatch):
    import json
    import services.telegram_bot as tb
    picks = [_pick(1, "totals", "over", 180.5, odds=2.0, fair=1.8), _pick(2, "totals", "under", 180.5, odds=2.0, fair=1.8)]
    monkeypatch.setattr(tb.repo, "all_picks", lambda n=0: picks)
    monkeypatch.setattr(tb.repo, "kv_get", lambda k: json.dumps({"1": {"res": "won"}, "2": {"res": "lost"}}))
    bot = tb.TelegramBot.__new__(tb.TelegramBot)
    out = bot.cmd_winrate("")
    assert "WIN RATE" in out and "2 graded" in out and "won 1 (50.0%)" in out
    assert "/winrate" not in out
    assert "winrate" in [c for c, _ in tb.COMMANDS]


def test_winrate_command_is_honest_when_nothing_graded(monkeypatch):
    import services.telegram_bot as tb
    monkeypatch.setattr(tb.repo, "all_picks", lambda n=0: [])
    monkeypatch.setattr(tb.repo, "kv_get", lambda k: None)
    out = tb.TelegramBot.__new__(tb.TelegramBot).cmd_winrate("")
    assert "Nothing graded yet" in out
