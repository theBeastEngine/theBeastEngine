"""Wiring tests for perception / validator / scraper bridge / subscription. No DB or network needed."""
import asyncio
import os
import sys
import types

os.environ.setdefault("TELEGRAM_CHAT_ID", "12345")

try:
    import sqlalchemy  # noqa: F401
except ImportError:  # sandbox without SQLAlchemy: stub only what finalize() touches
    _r = types.ModuleType("storage.repository")
    _r.recently_sent = lambda *a, **k: False
    _r.bets_today_on_book = lambda b: 0
    _r.bankroll_current = lambda: 100000.0
    sys.modules.setdefault("storage.repository", _r)

from config.settings import SETTINGS
from core.bookmakers import make_row, clean_rows, get_adapter
from core.edge_calculator import line_edge
from core.filter import Ctx, finalize, reject_reason
from core.market_selector import iter_outcomes
from core.perception import perception_report, detect_inefficiency
from core.subscription import is_authorized
from services.validator import validate_signal


def pick(i=1, ev_odds=2.0, p=0.55, **kw):
    d = dict(league="nba", event_id=f"e{i}", home="Boston", away="Miami", market="h2h", selection="home",
             side="home", line=None, p=p, odds=ev_odds, book="x")
    d["ev"] = p * ev_odds - 1
    d.update(kw)
    return d


def test_line_edge():
    assert line_edge("totals", "over", 220.5, 219.5) == 1.0
    assert line_edge("totals", "under", 220.5, 221.5) == 1.0
    assert line_edge("spreads", "home", -6.5, -5.5) == 1.0
    assert line_edge("spreads", "away", -6.5, -7.5) == 1.0
    assert line_edge("h2h", "home", None, None) is None


def test_rows_validation():
    assert make_row("m", "h2h", "home", 1.9, 3.0, "b")["line"] is None
    assert make_row("m", "spreads", "home", 1.9, None, "b") is None
    assert make_row("m", "totals", "side", 1.9, 200, "b") is None
    assert make_row("m", "h2h", "home", 1.0, None, "b") is None
    assert len(clean_rows([dict(market="h2h", side="away", odds=2.1, book="b", home="a"), {"market": "x"}])) == 1


def test_validator_and_filters():
    assert validate_signal(pick())
    assert not validate_signal(pick(ev_odds=1.8, p=0.55))          # EV < floor
    assert not validate_signal(pick(home="eSoccer Celtics"))        # fake
    assert reject_reason(pick(ev_odds=6.0, p=0.5)) == "liability"
    assert reject_reason(pick(book_margin=0.2)) == "margin_trap"
    assert detect_inefficiency(pick()) and not detect_inefficiency(pick(book_margin=0.2))


def test_perception_reasons_and_ctx():
    ctx = Ctx()
    kept, why = perception_report([pick(1), pick(2, ev_odds=1.5), pick(3, ev_odds=6.0, p=0.5)], ctx=ctx, remember=False)
    assert [k["event_id"] for k in kept] == ["e1"] and why == {"below_min_ev": 1, "liability": 1}
    ctx.remember(kept)
    kept2, why2 = perception_report([pick(1)], ctx=ctx, remember=False)
    assert kept2 == [] and why2 == {"duplicate_or_recent": 1}
    kept3, _ = perception_report([pick(1, ev_odds=2.2)], ctx=ctx, remember=False)  # price moved 10%: resend
    assert len(kept3) == 1


def test_finalize_remembers_only_sent(monkeypatch):
    monkeypatch.setenv("SCAN_CAP_PREMATCH", "5")  # step 2: the overall cap is a safety net, set to 5 for this check
    ctx = Ctx()
    cands = [pick(i, ev_odds=2.0 + i * 0.01) for i in range(7)]
    first = finalize([dict(c) for c in cands], "prematch", bankroll=100000, ctx=ctx)
    assert len(first) == 5 and all(p["stake"] == 5000 for p in first)
    second = finalize([dict(c) for c in cands], "prematch", bankroll=100000, ctx=ctx)
    assert len(second) == 2  # the two cut by top-N come through next scan
    assert {p["event_id"] for p in first}.isdisjoint({p["event_id"] for p in second})


def test_margin_attached_to_outcomes():
    ev = dict(event_id="x", league="nba", home="a", away="b", markets={"h2h": [
        dict(book="k", selection="home", line=None, odds=1.5), dict(book="k", selection="away", line=None, odds=1.5)]})
    out = list(iter_outcomes(ev))
    assert round(out[0]["book_margin"], 3) == 0.333 and out[0]["side"] == "home"


def test_scraper_bridge_end_to_end():
    from core.bookmakers import soft_feed
    import services.scraper as sc
    fake = types.ModuleType("core.bookmakers.fakebook")

    async def fetch_native(league):
        return [dict(match_id="m1", market="h2h", side="home", odds=1.8, line=None, book="fakebook", home="Boston", away="Miami"),
                dict(match_id="m1", market="spreads", side="home", odds=1.9, line=-5.5, book="fakebook", home="Boston", away="Miami"),
                dict(match_id="m1", market="h2h", side="home", odds=0.5, line=None, book="fakebook", home="Boston", away="Miami")]

    async def fetch_native_live(league, home, away):
        return [dict(match_id="L", market="totals", side="over", odds=1.95, line=221.5, book="fakebook")]

    fake.fetch_native, fake.fetch_native_live = fetch_native, fetch_native_live
    sys.modules["core.bookmakers.fakebook"] = fake
    old = sc.SOFT_BOOKS
    sc.SOFT_BOOKS = ["fakebook"]
    try:
        evs = soft_feed.upcoming("nba")
        assert len(evs) == 1 and evs[0]["home"] == "Boston" and len(evs[0]["markets"]["h2h"]) == 1
        assert evs[0]["markets"]["spreads"][0]["line"] == -5.5
        lv = soft_feed.live_game("nba", "Boston", "Miami")
        assert lv[0]["markets"]["totals"][0]["odds"] == 1.95 and lv[0]["away"] == "Miami"
    finally:
        sc.SOFT_BOOKS = old
        del sys.modules["core.bookmakers.fakebook"]


def test_native_adapters_expose_fetch_odds():
    for book in SETTINGS.SOFT_BOOKS:
        mod = get_adapter(book)
        assert mod is not None and mod.BOOK == book and callable(mod.fetch_odds), book


def test_subscription():
    assert is_authorized(SETTINGS.TELEGRAM_CHAT_ID) == bool(str(SETTINGS.TELEGRAM_CHAT_ID).strip())
    assert not is_authorized("999999999") and not is_authorized("")
