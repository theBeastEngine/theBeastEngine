from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core import livepin
from core.pinref import PinRef, _now
from core.beast import Beast


def _ev(prices, lg="pba", home="Alpha", away="Beta", commence=None):
    """prices: {(market, line, selection): odds} -> a normalized Pinnacle event."""
    markets = {}
    for (m, ln, sel), o in prices.items():
        markets.setdefault(m, []).append(dict(book="pinnacle", selection=sel, line=ln, odds=o))
    return dict(event_id="e1", league=lg, home=home, away=away, commence=commence, markets=markets)


PRE = _ev({("totals", 180.5, "over"): 1.90, ("totals", 180.5, "under"): 1.90,
           ("spreads", -4.5, "home"): 1.91, ("spreads", -4.5, "away"): 1.91})
LIVE_SAME = _ev({("totals", 180.5, "over"): 1.90, ("totals", 180.5, "under"): 1.90,
                 ("spreads", -4.5, "home"): 1.91, ("spreads", -4.5, "away"): 1.91})
LIVE = _ev({("totals", 150.5, "over"): 1.55, ("totals", 150.5, "under"): 2.50,
            ("spreads", -2.5, "home"): 1.95, ("spreads", -2.5, "away"): 1.87})


def pick(**kw):
    p = dict(market="totals", selection="over", line=150.5, odds=1.75, p=0.70, ev=0.22, league="pba",
             home="Alpha", away="Beta", mode="live", fair_odds=1.6)
    p.update(kw)
    return p


def test_frozen_prices_are_not_live_prices():
    assert livepin.frozen(LIVE_SAME, PRE) is True
    assert livepin.frozen(LIVE, PRE) is False
    assert livepin.frozen(LIVE, None) is False


def test_confirmed_pick_takes_the_real_probability():
    act, q, why = livepin.verify(pick(), LIVE, PRE, md="soft", min_ev=0.04, min_p=0.55, max_ev=0.5)
    assert act == "keep" and q["pin_live"] is True and abs(q["p"] - 0.6168) < 0.01   # 1/1.55 vs 1/2.5, devigged
    assert abs(q["ev"] - (q["p"] * 1.75 - 1)) < 1e-9 and q["p_model"] == 0.70


def test_live_pinnacle_without_edge_drops_the_pick():
    act, _q, why = livepin.verify(pick(odds=1.60), LIVE, PRE, md="soft", min_ev=0.04, min_p=0.5, max_ev=0.5)
    assert act == "drop" and "live Pinnacle" in why


def test_no_live_data_soft_keeps_strict_drops():
    act, q, _ = livepin.verify(pick(), None, PRE, md="soft")
    assert act == "keep" and q["pin_live"] is False
    assert livepin.verify(pick(), None, PRE, md="strict")[0] == "drop"
    assert livepin.verify(pick(), LIVE_SAME, PRE, md="strict")[0] == "drop"     # frozen = no live price
    assert livepin.verify(pick(line=99.5), LIVE, PRE, md="strict")[0] == "drop"  # line not carried live
    assert livepin.verify(pick(), None, PRE, md="off")[0] == "keep"


def test_mode_env(monkeypatch):
    monkeypatch.delenv("LIVE_PIN_VERIFY", raising=False)
    assert livepin.mode() == "soft"
    monkeypatch.setenv("LIVE_PIN_VERIFY", "STRICT")
    assert livepin.mode() == "strict"
    monkeypatch.setenv("LIVE_PIN_VERIFY", "nonsense")
    assert livepin.mode() == "soft"


def _pinref(tmp_path, monkeypatch, fetched):
    monkeypatch.setenv("ODDSPAPI_KEYS", "k1")
    pr = PinRef(api=None, path=str(tmp_path / "pin.json"))
    start = (_now() - timedelta(minutes=40)).isoformat()
    pre = dict(PRE, commence=start)
    pr.data["leagues"]["pba"] = {"fetched_at": _now().isoformat(), "events": [pre], "source": "oddspapi"}
    calls = []

    def fake(tids, league):
        calls.append(tids)
        pr.data["papi_spend"] = {"day": _now().date().isoformat(), "calls": pr._papi_spent_today() + 1}
        return [dict(startTime=start, statusId=2, hasOdds=True)], fetched, {}, None
    monkeypatch.setattr(pr, "_papi_fetch_or_empty", fake)
    monkeypatch.setattr(pr, "_live_tids", lambda lg: [250])
    return pr, calls, start


def test_live_event_is_cached_and_capped(tmp_path, monkeypatch):
    live = dict(LIVE, commence=_now() - timedelta(minutes=40))
    pr, calls, start = _pinref(tmp_path, monkeypatch, [live])
    ev = dict(league="pba", home="Alpha", away="Beta", commence=start)
    got, why = pr.live_event(ev)
    assert got is live and why is None
    pr.live_event(ev)
    assert len(calls) == 1                                   # second look inside LIVE_PIN_MIN_SEC is free
    monkeypatch.setenv("LIVE_PIN_DAILY_CAP", "0")
    pr._live_snap.clear()
    got, why = pr.live_event(ev)
    assert got is None and "cap" in why and len(calls) == 1  # cap reached: no spend


def test_live_event_unknown_game_and_no_key(tmp_path, monkeypatch):
    pr, _calls, start = _pinref(tmp_path, monkeypatch, [dict(LIVE, home="Other", away="Team", commence=_now())])
    got, why = pr.live_event(dict(league="pba", home="Alpha", away="Beta", commence=start))
    assert got is None and "not in the live Pinnacle list" in why
    monkeypatch.delenv("ODDSPAPI_KEYS", raising=False)
    monkeypatch.delenv("ODDSPAPI_KEY", raising=False)
    assert pr.live_fetch("pba")[0] is None


def test_probe_says_frozen_or_live(tmp_path, monkeypatch):
    pr, _c, _s = _pinref(tmp_path, monkeypatch, [dict(LIVE_SAME, commence=_now() - timedelta(minutes=40))])
    rows, notes = pr.live_probe(["pba"])
    assert rows and "FROZEN" in rows[0]["verdict"]
    assert "FROZEN" in livepin.probe_text(rows, notes)
    pr2, _c2, _s2 = _pinref(tmp_path, monkeypatch, [dict(LIVE, commence=_now() - timedelta(minutes=40))])
    rows, notes = pr2.live_probe(["pba"])
    assert rows and rows[0]["verdict"].startswith("LIVE") and rows[0]["total_now"] == 150.5 and rows[0]["total_pre"] == 180.5


def test_beast_hook_keeps_drops_and_labels(monkeypatch):
    monkeypatch.setenv("LIVE_PIN_VERIFY", "soft")
    live = dict(LIVE, commence=_now())
    fake_pr = SimpleNamespace(live_event=lambda p: (live, None), match=lambda p, allow_started=False: PRE)
    b = Beast.__new__(Beast)
    b.pinref = fake_pr
    good = pick()
    bad = pick(selection="under", odds=2.50, p=0.6, ev=0.5)
    out = b._live_pin_verify([good, bad])
    assert len(out) == 1 and out[0]["pin_live"] is True
    fake_pr.live_event = lambda p: (None, "this league has no OddsPapi source")
    out = b._live_pin_verify([good])
    assert out[0]["pin_live"] is False and "no OddsPapi" in out[0]["pin_live_note"]
    monkeypatch.setenv("LIVE_PIN_VERIFY", "strict")
    assert b._live_pin_verify([good]) == []
    monkeypatch.setenv("LIVE_PIN_VERIFY", "off")
    assert b._live_pin_verify([good]) == [good]


def test_alert_text_says_pinnacle_live_or_model_only():
    from core import telegram_notify as tn
    base = dict(id=7, mode="live", league="nba", home="Boston", away="Miami", market="totals", selection="over", line=220.5,
                book="sportybet", odds=1.95, fair_odds=1.85, min_odds=1.91, ev=0.05, p=0.6, stake=5000.0,
                commence_time="2026-10-20T00:30:00Z", period=3, clock="7:30", home_score=60, away_score=58)
    assert "PINNACLE LIVE" in tn.format_pick(dict(base, pin_live=True)) and "MODEL ONLY" not in tn.format_pick(dict(base, pin_live=True))
    assert "MODEL ONLY" in tn.format_pick(dict(base, pin_live=False))
    assert "MODEL ONLY" in tn.format_game_alert([dict(base, pin_live=False)])
    assert "PINNACLE LIVE" in tn.format_game_alert([dict(base, pin_live=True)])


def test_match_check(monkeypatch):
    from core import beast as bm
    start = _now() + timedelta(hours=5)
    ev = dict(event_id="e1", league="kbl", home="Wonju DB New Generation", away="Samsung Thunders", commence=start.isoformat())
    monkeypatch.setattr(bm.soft_feed, "upcoming", lambda lg: [ev] if lg == "kbl" else [])
    near = [dict(league="kbl", event_id="e1", home=ev["home"], away=ev["away"], market="totals", selection="over", line=176.5, odds=1.87, p=0.56, ev=0.047),
            dict(league="kbl", event_id="e1", home=ev["home"], away=ev["away"], market="h2h", selection="home", line=None, odds=1.40, p=0.70, ev=-0.02)]

    def cands(e, near=None):
        near.extend(near_src)
        return []
    near_src = near
    b = Beast.__new__(Beast)
    b.pinref = SimpleNamespace(candidates=cands)
    monkeypatch.setattr(b, "_has_pin", lambda e: True, raising=False)
    t = b.match_check("wonju")
    assert "MATCH CHECK" in t and "Wonju DB New Generation" in t and "Over 176.5" in t and "✅" in t and "\U0001F536" in t
    assert t.index("Over 176.5") < t.index("Wonju DB New Generation to win")           # qualifying first
    assert "No upcoming game" in b.match_check("zzzz")
    assert "Send a team" in b.match_check("")
    monkeypatch.setattr(b, "_has_pin", lambda e: False, raising=False)
    assert "No Pinnacle price for this game yet" in b.match_check("kbl")


def test_new_commands_are_registered():
    from services import telegram_bot as tb
    names = [c for c, _ in tb.COMMANDS]
    assert "match" in names and "livepin" in names
    for n in names:
        assert hasattr(tb.TelegramBot, "cmd_" + n)
