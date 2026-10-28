import os
from datetime import datetime, timezone

from core import blocks as B, dayreport as D, reasons as R
from core.shadow import Shadow


def _ep(h, mi=0, day=20):
    # Lagos is UTC+1 (ALERT_TZ_OFFSET_HOURS default 1)
    return datetime(2026, 10, day, tzinfo=timezone.utc).timestamp() + (h - 1) * 3600 + mi * 60


def _pick(p=0.62, odds=1.95, ev=0.11, **kw):
    d = dict(market="totals", selection="over", line=172.5, odds=odds, p=p, ev=ev, fair_odds=round(1 / p, 3),
             book_margin=0.06, move=0.01, home="A", away="B")
    d.update(kw)
    return d


def _match(i, tip, holding=True, p=0.62):
    mk = "totals|over|172.5"
    meta = {mk: dict(id=i, desc="Total: Over 172.5", last=_pick(p=p))}
    chk = dict(rows=[_pick(p=p)] if holding else [])
    return dict(key="k%d" % i, league="nbl", home="Home%d" % i, away="Away%d" % i, tip=tip, checks=[chk],
                play_keys=[mk], picks=meta, skipped=False)


def test_block_windows_are_three_hours_from_6am():
    assert B.block_of(_ep(6, 0))[1] == "6am–9am"
    assert B.block_of(_ep(8, 59))[1] == "6am–9am"
    assert B.block_of(_ep(9, 0))[1] == "9am–12pm"
    assert B.block_of(_ep(21, 30))[1] == "9pm–12am"
    assert B.block_of(_ep(0, 30))[1] == "12am–3am"
    assert B.block_of(_ep(5, 59))[1] == "3am–6am"


def test_one_card_per_block_edited_in_place_and_ticket_line():
    sent, edits = [], []
    b = B.Blocks(send=lambda t: sent.append(t) or 7, edit=lambda mid, t: edits.append((mid, t)) or True)
    ms = {"a": _match(1, _ep(19)), "b": _match(2, _ep(20, 30)), "c": _match(3, _ep(10))}
    assert b.update(ms) == 2                                   # 6pm-9pm card and 9am-12 card
    assert len(sent) == 2
    evening = [t for t in sent if "6pm–9pm" in t][0]
    assert "Suggested ticket (2 legs)" in evening and "Combined odds 3.80" in evening
    assert "Pinnacle 62%, SportyBet pays like 51%" in evening
    assert b.update(ms) == 0                                   # nothing changed: no edit
    ms["b"] = _match(2, _ep(20, 30), holding=False)      # one edge gone
    assert b.update(ms) == 1 and "<s>" in edits[-1][1] and "No ticket yet" in edits[-1][1]


def test_ticket_needs_two_different_games_with_enough_win_chance():
    m1, m2 = _match(1, _ep(19)), _match(2, _ep(20), p=0.58)
    live = [(m1, [_pick()]), (m2, [_pick(p=0.58)])]
    assert B.ticket(live) is None                              # second leg under 60%
    assert B.ticket([(m1, [_pick(), _pick(p=0.7)])]) is None   # two picks of ONE game are never combined
    t = B.ticket([(m1, [_pick()]), (m2, [_pick(p=0.65)]), (_match(3, _ep(21)), [_pick(p=0.61)])])
    assert len(t["legs"]) == 3 and abs(t["odds"] - 1.95 ** 3) < 1e-9


def test_why_lines_say_which_rule_each_market_misses():
    near = [_pick(p=0.61, ev=0.019), _pick(p=0.52, ev=0.05, market="spreads", selection="home", line=-1.5),
            _pick(p=0.60, ev=0.06, book_margin=0.15, market="h2h", selection="away", line=None)]
    lines = R.why_lines([], near)
    assert len(lines) == 3 and lines[0].startswith("Moneyline") and "SportyBet margin 15%, over the 12% limit" in lines[0]
    assert any("win chance 52%, needs 55%" in x for x in lines)
    assert any("edge +1.9%, needs +4%" in x for x in lines)


def test_day_report_covers_yesterday_only():
    now = _ep(8, 5, day=20)
    y = _ep(20, 0, day=19)
    picks = [dict(id=1, mode="prematch", commence=datetime.fromtimestamp(y, tz=timezone.utc), status="won", stake=2000,
                  took_odds=1.9, book_odds=1.9, fair_odds=1.7, league="nbl", home="A", away="B"),
             dict(id=2, mode="prematch", commence=datetime.fromtimestamp(y, tz=timezone.utc), status="lost", stake=1000,
                  took_odds=1.9, book_odds=1.9, fair_odds=1.7, league="nbl", home="C", away="D"),
             dict(id=3, mode="prematch", commence=datetime.fromtimestamp(_ep(20, 0, day=20), tz=timezone.utc), status="new",
                  book_odds=1.9, fair_odds=1.7, league="nbl", home="E", away="F")]
    sh = Shadow(load=lambda: None, save=lambda v: None)
    t = D.build(now, picks, {}, sh, 100000)
    assert "2 taken" in t and "won 1" in t and "lost 1" in t and "+\u20a6800" in t
    assert "Bankroll ₦100,000" in t


def test_day_report_due_once_after_8am():
    assert not D.due(_ep(7, 30), None)
    assert D.due(_ep(8, 1), None)
    assert not D.due(_ep(9, 0), "20261020")
    assert D.due(_ep(9, 0), "20261019")


# ---- close calls: one side per market per day; quarter phases; icons every phone can draw
def test_close_calls_never_flip_between_the_two_sides_of_a_market():
    from core.reports import CloseCalls
    cc = CloseCalls()
    base = dict(league="nba", event_id="e1", home="A", away="B", market="h2h", line=None, odds=1.9, p=0.5, ev=-0.02)
    t = 1_792_300_000.0
    for i in range(2):
        cc.update("prematch", [dict(base, selection="home")], now=t + i * 1800)
    assert [e["item"]["selection"] for _g, es in cc.entries("prematch") for e in es] == ["home"]
    for i in range(2, 10):          # home disappears, the other side now looks close: it must NOT take its place today
        cc.update("prematch", [dict(base, selection="away")], now=t + i * 1800)
    assert cc.entries("prematch") == []
    cc.update("prematch", [dict(base, selection="away")], now=t + 86400 + 3600 * 3)
    cc.update("prematch", [dict(base, selection="away")], now=t + 86400 + 3600 * 3 + 1800)
    assert [e["item"]["selection"] for _g, es in cc.entries("prematch") for e in es] == ["away"]   # next day it may


def test_quarter_messages_default_to_q1_and_q3(monkeypatch):
    from core import quarters
    monkeypatch.delenv("QUARTER_PHASES", raising=False)
    assert quarters.sent_phases() == {"1", "3"}
    monkeypatch.setenv("QUARTER_PHASES", "1,2,3,4")
    assert quarters.sent_phases() == {"1", "2", "3", "4"}


def test_no_icon_needs_a_new_emoji_font():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    bad = [str(p) for d in ("core", "services") for p in (root / d).rglob("*.py")
           if any(("\\U0001F7E%X" % i) in p.read_text() for i in range(16))]
    assert bad == []          # U+1F7E0..1F7EF (coloured dots) show as empty circles on older Android


def test_dead_odds_api_key_rests_goes_last_and_comes_back():
    from services import odds_api as oa
    box = {}
    bk = oa.book()
    bk.load, bk.save = (lambda: box.get("v")), (lambda v: box.__setitem__("v", v))
    api = oa.OddsAPI(keys=["k1", "k2", "k3"])
    assert api._key() == "k1"
    api._dead("k1", 401)
    api._dead("k2", 401)
    assert api._key() == "k3"
    assert [r[0] for r in bk.rows(["k1", "k2", "k3"])][0] == "k3" and "k1" not in box["v"]     # dead last; no raw key stored
    api2 = oa.OddsAPI(keys=["k1", "k2", "k3"])                    # a restart: a new client reads the saved state
    bk._d = None
    assert api2._key() == "k3"
    api2._dead("k3", 401)
    try:
        api2._key()
        assert False
    except oa.OutOfCredits:
        pass
    assert bk.summary(["k1", "k2", "k3"]) == (3, 0, 3)
    for h in list(bk._s()["dead"]):                               # rest day over: tried again, in the original order
        bk._s()["dead"][h] = 1
    assert api2._key() == "k1"


def _quiet(tip, why="Moneyline: Home to win | 62% | edge +1.9%, needs +4%. Pinnacle 62%, SportyBet pays like 60%"):
    return dict(key="q%d" % tip, league="nba", home="Quiet Home", away="Quiet Away", tip=tip, play_keys=[], picks={}, slots=[],
                checks=[dict(status="watch", why=[why], nxt=tip - 3600, kind="check")])


def test_every_window_gets_a_card_even_with_no_play_now():
    sent, edits = [], []
    tip = _ep(19, 30)
    now_far = _ep(12)                       # 6pm-9pm window starts in 6 hours: no card yet
    b = B.Blocks(send=lambda t: sent.append(t) or 9, edit=lambda mid, t: edits.append((mid, t)) or True, now=lambda: now_far)
    assert b.update({"a": _quiet(tip)}) == 0 and not sent
    b2 = B.Blocks(send=lambda t: sent.append(t) or 9, edit=lambda mid, t: edits.append((mid, t)) or True, now=lambda: _ep(17, 30))
    assert b2.update({"a": _quiet(tip)}) == 1                   # one hour before the window: the card goes out
    txt = sent[-1]
    assert "NO PLAY NOW YET" in txt and "Quiet Home" in txt and "edge +1.9%, needs +4%" in txt and "next check" in txt
    assert "Suggested ticket" not in txt


def test_morning_list_is_league_by_league_for_the_next_24_hours():
    from core import agenda
    from datetime import datetime, timezone
    now = _ep(8, 0)
    ev = lambda lg, h, a, t: (lg, dict(league=lg, home=h, away=a, commence=datetime.fromtimestamp(t, tz=timezone.utc).replace(tzinfo=None)))
    events = [ev("nba", "Spurs", "Thunder", _ep(23)), ev("nba", "Knicks", "76ers", _ep(21)), ev("pba", "RoS", "Tim", _ep(12)),
              ev("nba", "Too", "Late", _ep(8, 0) + 30 * 3600)]
    msgs = agenda.build(now, events, lambda e: e["league"] == "nba")
    t = "\n".join(msgs)
    assert "3 matches in 2 leagues" in t and "Pinnacle price on 2" in t
    assert t.index("Knicks") < t.index("Spurs") and "Too" not in t
    assert t.index("PBA") < t.index("NBA") or t.index("NBA") < t.index("PBA")
    assert "✅" in t and "➖" in t
