"""Step 2: per-game multi-market selection, win-probability floor, no contradictions, labels."""
import re
from core import gates
from core.telegram_notify import format_pick


def c(market, sel, line, ev, p, eid="g1", odds=2.0):
    return dict(league="nba", event_id=eid, home="A", away="B", market=market, selection=sel, side=sel,
                line=line, ev=ev, p=p, odds=odds)


def test_win_prob_floor_defaults(monkeypatch):
    for k in ("MIN_WIN_PROB_PREMATCH", "MIN_WIN_PROB_SIM", "MIN_WIN_PROB_LIVE"):
        monkeypatch.delenv(k, raising=False)
    assert gates.passes_win_prob(c("h2h", "home", None, .05, .55), "prematch")
    assert not gates.passes_win_prob(c("h2h", "home", None, .05, .549), "prematch")
    assert not gates.passes_win_prob(c("h2h", "home", None, .05, .59), "sim")
    assert gates.passes_win_prob(c("h2h", "home", None, .05, .60), "live")
    assert not gates.passes_win_prob({"p": None}, "prematch")


def test_win_prob_env_override(monkeypatch):
    monkeypatch.setenv("MIN_WIN_PROB_PREMATCH", "0.70")
    assert not gates.passes_win_prob(c("h2h", "home", None, .05, .65), "prematch")


def test_over_and_under_never_together():
    assert gates.conflicts(c("totals", "over", 220.5, .05, .6), c("totals", "under", 222.5, .05, .6))
    assert gates.conflicts(c("totals", "over", 220.5, .05, .6), c("totals", "over", 222.5, .05, .6))


def test_ml_one_team_spread_other_conflicts():
    assert gates.conflicts(c("h2h", "home", None, .05, .6), c("spreads", "away", 3.5, .05, .6))
    assert not gates.conflicts(c("h2h", "home", None, .05, .6), c("spreads", "home", -3.5, .05, .6))


def test_periods_are_independent():
    assert not gates.conflicts(c("totals", "over", 220.5, .05, .6), c("totals_h1", "under", 110.5, .05, .6))


def test_select_for_game_caps_and_orders(monkeypatch):
    for k in ("PER_GAME_PREMATCH", "PER_GAME_SIM", "PER_GAME_LIVE"):
        monkeypatch.delenv(k, raising=False)
    cands = [c("totals", "over", 220.5, .06, .6), c("totals", "under", 222.5, .05, .6),
             c("spreads", "home", -3.5, .05, .6), c("h2h", "away", None, .05, .6),
             c("h2h_h1", "home", None, .05, .6), c("spreads_h1", "home", -1.5, .05, .6),
             c("totals_h1", "over", 110.5, .05, .6), c("spreads_q1", "home", -1.5, .05, .6)]
    cands.sort(key=gates.score, reverse=True)
    kept = gates.select_for_game(cands, "prematch")
    assert len(kept) == 5
    assert sum(1 for k in kept if k["market"] == "totals") == 1 and kept[0]["market"] == "totals"
    assert not any(k["market"] == "h2h" for k in kept)       # away ML contradicts home spread
    assert len(gates.select_for_game(cands, "sim")) == 3
    # live keeps up to 3 per game so the stance gate sees every family; run_live then sends at most one per game
    assert len(gates.select_for_game(cands, "live")) == 3


def test_priority_outranks_equal_ev():
    a, b = c("h2h", "home", None, .05, .6), c("totals", "over", 220.5, .05, .6)
    assert gates.score(b) > gates.score(a)
    assert gates.priority_name(b) == "High" and gates.priority_name(a) == "Standard"


def test_labels_in_alert():
    base = dict(id=1, mode="sim", league="nba", home="A", away="B", market="totals_h1", selection="over", line=110.5,
                book="sportybet", odds=1.95, fair_odds=1.8, min_odds=1.9, ev=.05, p=.6, stake=5000.0,
                pin_real=True, priority="High")
    t = re.sub(r"</?(b|i)>", "", format_pick(base))
    assert "Type of Match: Simulation" in t and "League on Pinnacle: Yes" in t
    assert "BET: Total (1st half) Over 110.5" in t and "Priority: High" in t
    assert "No (model priors only)" in re.sub(r"</?(b|i)>", "", format_pick({**base, "pin_real": False}))
    assert "Type of Match: Pre-Match" in re.sub(r"</?(b|i)>", "", format_pick({**base, "mode": "prematch"}))
    assert "Type of Match: Live Match" in re.sub(r"</?(b|i)>", "", format_pick({**base, "mode": "live"}))


# ---- Sim / Live confirmation and Sim shrinkage ----
class _C:
    pass


def test_confirmation_needs_two_scans(monkeypatch):
    monkeypatch.delenv("PERSIST_WINDOW_LIVE", raising=False)
    ctx = _C()
    a = c("totals", "over", 220.5, .06, .6)
    assert gates.confirm_persistent([a], "live", ctx, now=1000.0) == []            # first sighting: held back
    out = gates.confirm_persistent([a], "live", ctx, now=1120.0)                    # second scan 2 min later
    assert len(out) == 1 and out[0]["confirmed_scans"] == 2


def test_confirmation_expires_and_prematch_is_never_delayed(monkeypatch):
    ctx = _C()
    a = c("totals", "over", 220.5, .06, .6)
    gates.confirm_persistent([a], "live", ctx, now=1000.0)
    assert gates.confirm_persistent([a], "live", ctx, now=1000.0 + 10_000) == []   # too old: starts again
    assert gates.confirm_persistent([a], "prematch", _C(), now=5.0) == [a]


def test_confirmation_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("PERSIST_WINDOW_LIVE", "0")
    a = c("totals", "over", 220.5, .06, .6)
    assert gates.confirm_persistent([a], "live", _C(), now=1.0) == [a]


def test_confirmation_is_per_selection(monkeypatch):
    monkeypatch.delenv("PERSIST_WINDOW_SIM", raising=False)
    ctx = _C()
    a, b = c("totals", "over", 220.5, .06, .6), c("totals", "under", 220.5, .06, .6)
    gates.confirm_persistent([a], "sim", ctx, now=1.0)
    out = gates.confirm_persistent([a, b], "sim", ctx, now=2.0)
    assert [x["selection"] for x in out] == ["over"]   # the under was never seen before


def test_shrink_blends_toward_pinnacle_and_drops_unpriced_lines():
    fair = {("totals", 220.5): {"over": 0.50, "under": 0.50}}
    hit = c("totals", "over", 220.5, .06, .60)
    miss = c("totals", "over", 224.5, .06, .70)
    out = gates.shrink_sim([hit, miss], fair, w=0.5, need_line=True)
    assert len(out) == 1 and abs(out[0]["p"] - 0.55) < 1e-9 and out[0]["p_model"] == .60 and out[0]["p_pin"] == .50
    out2 = gates.shrink_sim([hit, miss], fair, w=0.5, need_line=False)
    assert len(out2) == 2 and out2[1]["p"] == .70   # unpriced line kept untouched when the switch is off


def test_shrink_defaults(monkeypatch):
    monkeypatch.delenv("SIM_SHRINK", raising=False)
    monkeypatch.delenv("SIM_REQUIRE_PIN_LINE", raising=False)
    assert gates.shrink_weight() == 0.5 and gates.sim_needs_pin_line() is True
    monkeypatch.setenv("SIM_SHRINK", "9")
    assert gates.shrink_weight() == 1.0
