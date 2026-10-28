"""Injury hold/note, line-in-motion hold and two-scan pre-match confirmation (pure logic, no network, no database)."""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta

from core import confirm, injuries, steam


@contextmanager
def env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# These tests pin every setting they depend on, so they pass whatever the surrounding test run has switched off
# (the push cell runs the older tests with PREMATCH_CONFIRM_SEC=0 INJURIES=0 STEAM_HOLD=0).
PINNED = dict(PREMATCH_CONFIRM_SEC="900", PREMATCH_CONFIRM_GAP_SEC="60", INJURIES="1", INJURY_HOLD_MIN="60",
              INJURY_REFRESH_MIN="15", STEAM_HOLD="1", STEAM_WINDOW_MIN="30", STEAM_H2H="0.03", STEAM_LINE="1.0",
              STEAM_TOTAL="1.5")

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _pinned_env():
        with env(**PINNED):
            yield
except ImportError:   # plain runner
    pass


T0 = datetime(2026, 10, 20, 12, 0)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def add(self, minutes):
        self.t = self.t + timedelta(minutes=minutes)


def payload(teams):
    return {"injuries": [{"id": str(i), "displayName": name,
                          "injuries": [{"status": s, "athlete": {"displayName": p}} for p, s in plist]}
                         for i, (name, plist) in enumerate(teams.items())]}


# ---------------------------------------------------------------- injuries
def test_parse_skips_bad_entries():
    d = {"injuries": [{"displayName": "A Team", "injuries": [{"status": "Out", "athlete": {"displayName": "P1"}},
                                                             {"status": "Out"}, {"athlete": {"displayName": "P2"}}]},
                      {"injuries": []}, {"displayName": "B Team"}]}
    assert injuries.parse(d) == {"A Team": {"P1": "Out"}, "B Team": {}}
    assert injuries.parse(None) == {}


def test_same_team():
    assert injuries.same_team("LA Clippers", "Los Angeles Clippers")
    assert injuries.same_team("Portland Trail Blazers", "Portland Trail Blazers")
    assert not injuries.same_team("Los Angeles Lakers", "Los Angeles Clippers")
    assert not injuries.same_team("", "x")


def test_first_fetch_seeds_without_holding():
    clk, data = Clock(), {"v": payload({"Los Angeles Lakers": [("A", "Out")], "Sacramento Kings": []})}
    b = injuries.InjuryBook(fetch=lambda: data["v"], now=clk)
    assert b.hold_reason("nba", "Los Angeles Lakers", "Sacramento Kings") is None
    assert b.fresh == []


def test_change_triggers_hold_then_expires():
    clk, data = Clock(), {"v": payload({"Los Angeles Lakers": [("A", "Day-To-Day")], "Sacramento Kings": []})}
    b = injuries.InjuryBook(fetch=lambda: data["v"], now=clk)
    b.refresh()
    clk.add(20)
    data["v"] = payload({"Los Angeles Lakers": [("A", "Out")], "Sacramento Kings": []})
    why = b.hold_reason("nba", "LA Lakers", "Sacramento Kings")
    assert why and "A (Out)" in why and "0 min ago" in why
    clk.add(30)
    assert b.hold_reason("nba", "Los Angeles Lakers", "Sacramento Kings") is not None   # 30 min < 60
    clk.add(40)
    assert b.hold_reason("nba", "Los Angeles Lakers", "Sacramento Kings") is None       # 70 min > 60


def test_new_and_cleared_players_count_as_news():
    clk, data = Clock(), {"v": payload({"Boston Celtics": [("A", "Out")], "Miami Heat": []})}
    b = injuries.InjuryBook(fetch=lambda: data["v"], now=clk)
    b.refresh()
    clk.add(20)
    data["v"] = payload({"Boston Celtics": [], "Miami Heat": [("B", "Out")]})
    b.refresh()
    reasons = {f["player"]: f["status"] for f in b.fresh}
    assert reasons == {"A": "Cleared", "B": "Out"}


def test_other_games_and_leagues_not_held():
    clk, data = Clock(), {"v": payload({"Boston Celtics": [], "Miami Heat": []})}
    b = injuries.InjuryBook(fetch=lambda: data["v"], now=clk)
    b.refresh()
    clk.add(20)
    data["v"] = payload({"Boston Celtics": [("A", "Out")], "Miami Heat": []})
    assert b.hold_reason("nba", "Denver Nuggets", "Utah Jazz") is None
    assert b.hold_reason("euroleague", "Boston Celtics", "Miami Heat") is None
    assert b.note("euroleague", "Boston Celtics", "Miami Heat") is None


def test_note_text_and_limits():
    clk = Clock()
    pl = [("P%d" % i, "Out") for i in range(5)]
    data = payload({"Boston Celtics": pl + [("Q", "Day-To-Day")], "Miami Heat": []})
    b = injuries.InjuryBook(fetch=lambda: data, now=clk)
    n = b.note("nba", "Boston Celtics", "Miami Heat")
    assert n == "Injuries: Boston Celtics: Out P0, P1, P2 +2; day-to-day Q."
    assert injuries.InjuryBook(fetch=lambda: payload({"A B": [], "C D": []}), now=clk).note("nba", "A B", "C D") is None


def test_fail_open_and_switches():
    clk = Clock()

    def boom():
        raise OSError("blocked")
    b = injuries.InjuryBook(fetch=boom, now=clk)
    assert b.note("nba", "A", "B") is None and b.hold_reason("nba", "A", "B") is None
    ok = injuries.InjuryBook(fetch=lambda: payload({"Boston Celtics": [("A", "Out")]}), now=clk)
    with env(INJURIES="0"):
        assert ok.note("nba", "Boston Celtics", "Miami Heat") is None
    with env(INJURY_HOLD_MIN="0"):
        assert ok.hold_reason("nba", "Boston Celtics", "Miami Heat") is None


def test_stale_data_ignored_and_refresh_interval():
    clk, calls = Clock(), []

    def f():
        calls.append(1)
        return payload({"Boston Celtics": [("A", "Out")]})
    b = injuries.InjuryBook(fetch=f, now=clk)
    b.note("nba", "Boston Celtics", "Miami Heat")
    b.note("nba", "Boston Celtics", "Miami Heat")
    assert len(calls) == 1                      # refresh interval respected
    b._tried_at = clk() - timedelta(hours=20)   # data and last try are old, and the feed is now down
    b.fetched_at = clk() - timedelta(hours=20)
    b._fetch = lambda: (_ for _ in ()).throw(OSError("down"))
    assert b.note("nba", "Boston Celtics", "Miami Heat") is None


# ---------------------------------------------------------------- steam
def ev(h2h_home=2.0, h2h_away=1.8, spread=-5.5, total=220.5, eid="e1"):
    outs = {"h2h": [dict(book="pinnacle", selection="home", line=None, odds=h2h_home),
                    dict(book="pinnacle", selection="away", line=None, odds=h2h_away)],
            "spreads": [dict(book="pinnacle", selection="home", line=spread, odds=1.95),
                        dict(book="pinnacle", selection="away", line=spread, odds=1.95)],
            "totals": [dict(book="pinnacle", selection="over", line=total, odds=1.95),
                       dict(book="pinnacle", selection="under", line=total, odds=1.95)]}
    return {"event_id": eid, "home": "A", "away": "B", "markets": outs}


def fake_fair(e):
    """Stand-in for pinref.fair_probs: two-way devig of the Pinnacle outcomes."""
    out = {}
    for m, outs in e["markets"].items():
        a, b = outs[0], outs[1]
        pa = (1 / a["odds"]) / (1 / a["odds"] + 1 / b["odds"])
        out[(m, a["line"])] = {a["selection"]: pa, b["selection"]: 1 - pa}
    return out


def test_no_move_no_hold():
    clk = Clock()
    s = steam.SteamTracker(now=clk, fair_fn=fake_fair)
    s.record("nba", [ev()])
    clk.add(10)
    s.record("nba", [ev()])
    assert s.moving("nba", "e1") is None


def test_line_move_holds_then_releases():
    clk = Clock()
    s = steam.SteamTracker(now=clk, fair_fn=fake_fair)
    s.record("nba", [ev(spread=-5.5)])
    clk.add(10)
    s.record("nba", [ev(spread=-7.0)])
    why = s.moving("nba", "e1")
    assert why and "spread line -1.5" in why and "10 min" in why
    clk.add(25)   # the move is 25 min old: still inside the 30 min window, still held
    assert s.moving("nba", "e1") is not None
    clk.add(10)   # 35 min old and the line stayed quiet: released
    assert s.moving("nba", "e1") is None


def test_small_moves_ignored_and_thresholds_by_env():
    clk = Clock()
    s = steam.SteamTracker(now=clk, fair_fn=fake_fair)
    s.record("nba", [ev(spread=-5.5, total=220.5)])
    clk.add(5)
    s.record("nba", [ev(spread=-6.0, total=221.5)])
    assert s.moving("nba", "e1") is None
    with env(STEAM_LINE="0.5"):
        assert "spread" in s.moving("nba", "e1")
    with env(STEAM_HOLD="0"):
        assert s.moving("nba", "e1") is None


def test_moneyline_and_total_moves():
    clk = Clock()
    s = steam.SteamTracker(now=clk, fair_fn=fake_fair)
    s.record("nba", [ev(h2h_home=2.0, h2h_away=1.8, total=220.5)])
    clk.add(5)
    s.record("nba", [ev(h2h_home=1.7, h2h_away=2.2, total=223.0)])
    why = s.moving("nba", "e1")
    assert "moneyline" in why and "total line +2.5" in why


def test_unknown_game_and_event_key():
    s = steam.SteamTracker(now=Clock(), fair_fn=fake_fair)
    assert s.moving("nba", "nope") is None
    assert steam.event_key({"home": "A", "away": "B"}) == "A|B"
    assert steam.event_key({"event_id": 7, "home": "A", "away": "B"}) == "7"


# ---------------------------------------------------------------- two-scan confirmation
class Ctx:
    pass


def pick(**kw):
    return dict(dict(league="nba", event_id="e1", market="spreads", selection="home", line=-5.5, ev=0.05), **kw)


def test_first_sighting_not_sent_second_is():
    ctx = Ctx()
    assert confirm.confirm([pick()], "prematch", ctx, now=1000.0) == []
    out = confirm.confirm([pick()], "prematch", ctx, now=1130.0)
    assert len(out) == 1 and out[0]["confirmed_scans"] == 2


def test_two_calls_within_gap_do_not_confirm():
    ctx = Ctx()
    confirm.confirm([pick()], "prematch", ctx, now=1000.0)
    assert confirm.confirm([pick()], "prematch", ctx, now=1020.0) == []     # 20 s apart = the same scan
    assert len(confirm.confirm([pick()], "prematch", ctx, now=1100.0)) == 1  # 100 s after the first sighting


def test_window_expiry_restarts_confirmation():
    ctx = Ctx()
    confirm.confirm([pick()], "prematch", ctx, now=1000.0)
    assert confirm.confirm([pick()], "prematch", ctx, now=1000.0 + 901) == []   # window 900 s passed: starts over


def test_edge_must_be_same_bet():
    ctx = Ctx()
    confirm.confirm([pick()], "prematch", ctx, now=1000.0)
    assert confirm.confirm([pick(line=-6.5)], "prematch", ctx, now=1200.0) == []
    assert confirm.confirm([pick(selection="away")], "prematch", ctx, now=1300.0) == []


def test_switch_never_delayed_and_off_switch():
    ctx = Ctx()
    assert len(confirm.confirm([pick(switch_note="replaces #4")], "prematch", ctx, now=1000.0)) == 1
    with env(PREMATCH_CONFIRM_SEC="0"):
        assert len(confirm.confirm([pick()], "prematch", Ctx(), now=1000.0)) == 1
    assert confirm.confirm([pick()], "prematch", None, now=1000.0) == [pick()]


def test_other_modes_use_their_own_rule():
    class C:
        pass
    ctx = C()
    # live: first sighting held, second kept (core.gates.confirm_persistent); needs config, so stub it
    import sys
    import types
    fake = types.ModuleType("core.gates")
    fake.confirm_persistent = lambda ranked, mode, ctx, now=None: ("delegated", mode)
    real = sys.modules.get("core.gates")
    sys.modules["core.gates"] = fake
    try:
        import core
        old = getattr(core, "gates", None)
        core.gates = fake
        assert confirm.confirm([pick()], "live", ctx) == ("delegated", "live")
        assert confirm.confirm([pick()], "sim", ctx) == ("delegated", "sim")
    finally:
        if real is not None:
            sys.modules["core.gates"] = real
        else:
            sys.modules.pop("core.gates", None)
        if old is not None:
            core.gates = old
        elif hasattr(core, "gates"):
            del core.gates


def test_default_confirm_window_covers_the_scan_interval():
    with env(PREMATCH_CONFIRM_SEC=None):
        assert confirm.prematch_window() >= 3600   # pre-match scans run every 30 min
    ctx = Ctx()
    with env(PREMATCH_CONFIRM_SEC=None):
        assert confirm.confirm([pick()], "prematch", ctx, now=0.0) == []
        assert len(confirm.confirm([pick()], "prematch", ctx, now=1800.0)) == 1   # next 30-min scan confirms it


def test_implausible_line_jumps_are_ignored():
    clk = Clock()
    s = steam.SteamTracker(now=clk, fair_fn=fake_fair)
    s.record("pba", [ev(spread=-5.5, total=190.5)])
    clk.add(10)
    s.record("pba", [ev(spread=-5.5, total=76.5)])    # the 'main' total flipped to another line: -114
    assert s.moving("pba", "e1") is None
    clk.add(5)
    s.record("pba", [ev(spread=-5.5, total=193.5)])   # a real 3 point move from the original is still caught
    assert "total line" in (s.moving("pba", "e1") or "")
