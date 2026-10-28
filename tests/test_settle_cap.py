"""Per-match exposure cap + settling a whole game from its scores."""
from types import SimpleNamespace

import pytest

import services.telegram_bot as tb
from core import settle
from core.kelly import cap_match_stakes

P4 = [(28, 24), (30, 22), (25, 28), (29, 30)]     # 112-104 home; H1 58-46; H2 54-58; Q4 29-30


def pk(market, sel, line=None, **kw):
    d = dict(id=1, league="nba", market=market, selection=sel, line=line)
    d.update(kw)
    return d


# ---- cap ----
def test_cap_leaves_small_totals_alone():
    assert cap_match_stakes([2000, 2000], 100000, 0, pct=0.15) == [2000, 2000]


def test_cap_scales_all_stakes_down_and_floors_to_step():
    out = cap_match_stakes([5000] * 6, 100000, 0, pct=0.15)      # 30k wanted, 15k allowed
    assert out == [2500] * 6 and sum(out) <= 15000


def test_cap_counts_money_already_placed_on_the_game():
    out = cap_match_stakes([5000, 5000], 100000, 12000, pct=0.15)  # only 3k left
    assert sum(out) <= 3000


def test_cap_full_game_exposure_blocks_everything():
    assert cap_match_stakes([5000], 100000, 15000, pct=0.15) == [0]


def test_cap_off_with_zero_and_env(monkeypatch):
    assert cap_match_stakes([5000] * 9, 100000, 0, pct=0) == [5000] * 9
    monkeypatch.setenv("MATCH_EXPOSURE_PCT", "0.05")
    assert sum(cap_match_stakes([5000] * 4, 100000, 0)) <= 5000


# ---- settle ----
def test_moneyline_and_spread_full_game():
    assert settle.settle(pk("h2h", "home"), P4, None) == "won"
    assert settle.settle(pk("h2h", "away"), P4, None) == "lost"
    assert settle.settle(pk("spreads", "home", -7.5), P4, None) == "won"     # 8 > 7.5
    assert settle.settle(pk("spreads", "home", -8.5), P4, None) == "lost"
    assert settle.settle(pk("spreads", "away", -8.5), P4, None) == "won"     # home line -8.5, away +8.5 covers
    assert settle.settle(pk("spreads", "home", -8.0), P4, None) == "void"


def test_totals_and_push():
    assert settle.settle(pk("totals", "over", 215.5), P4, None) == "won"   # 216 > 215.5
    assert settle.settle(pk("totals", "under", 215.5), P4, None) == "lost"
    assert settle.settle(pk("totals", "over", 216.0), P4, None) == "void"


def test_overtime_counts_in_full_game_only_for_regulation_markets_it_should():
    ot = (5, 3)
    assert settle.settle(pk("h2h", "home"), [(20, 20)] * 4, ot) == "won"
    assert settle.settle(pk("totals", "over", 160.5), [(20, 20)] * 4, ot) == "won"   # 168 incl OT


def test_two_period_league():
    two = [(40, 35), (38, 30)]
    assert settle.settle(pk("h2h", "home", league="ncaab"), two, None) == "won"
    assert settle.settle(pk("totals", "over", 142.5), two, None) == "won"


def test_unsure_cases_return_none():
    assert settle.settle(pk("team_totals", "over", 100.5), P4, None) is None
    assert settle.settle(pk("totals", "over", 215.25), P4, None) is None
    assert settle.settle(pk("spreads", "home", None), P4, None) is None


def test_parse_scores_errors_and_ok():
    assert settle.parse_scores(["28-24", "30-22", "25-28", "29-30", "ot=5-3"], 4) == (P4, (5, 3))
    with pytest.raises(ValueError):
        settle.parse_scores(["28-24"], 4)
    with pytest.raises(ValueError):
        settle.parse_scores(["28:24", "1-1", "1-1", "1-1"], 4)
    with pytest.raises(ValueError):
        settle.parse_scores(["1-1"] * 4 + ["ot=x"], 4)


# ---- /score command ----
class Repo:
    def __init__(self, picks):
        self.picks, self.done, self.bank = picks, [], 100000.0

    def get_pick(self, pid):
        return next((p for p in self.picks if p["id"] == pid), None)

    def placed_picks_for_event(self, lg, ev):
        return [p for p in self.picks if p["status"] == "placed"]

    def settle_pick(self, pid, res):
        self.done.append((pid, res))
        return self.bank


def _bot(monkeypatch, picks):
    r = Repo(picks)
    monkeypatch.setattr(tb, "repo", r)
    return tb.TelegramBot(SimpleNamespace(paused=False)), r


def test_score_command_settles_the_whole_game(monkeypatch):
    picks = [pk("h2h", "home", id=1, event_id="e", status="placed"),
             pk("totals", "under", 215.5, id=2, event_id="e", status="placed"),
             pk("team_totals", "over", 100.5, id=3, event_id="e", status="placed"),
             pk("h2h", "away", id=4, event_id="e", status="new")]
    bot, r = _bot(monkeypatch, picks)
    out = bot.cmd_score("1 28-24 30-22 25-28 29-30")
    assert r.done == [(1, "won"), (2, "lost")]
    assert "#3" in out and "/result" in out


def test_score_command_usage_and_errors(monkeypatch):
    bot, r = _bot(monkeypatch, [pk("h2h", "home", id=1, event_id="e", status="placed")])
    assert "Usage" in bot.cmd_score("")
    assert "No pick #9" in bot.cmd_score("9 1-1 1-1 1-1 1-1")
    assert "4 periods" in bot.cmd_score("1 1-1 1-1")
    assert r.done == []


def test_score_is_in_menu_and_has_handler():
    assert "score" in [c for c, _ in tb.COMMANDS] and hasattr(tb.TelegramBot, "cmd_score")
