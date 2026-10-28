"""Live protection: blend toward the book price, EV caps, per-game cooldown/cap/no-opposite."""
from datetime import datetime, timedelta
from core import gates


def c(sel, p, odds=1.9, other=1.9, market="totals", line=150.5, **kw):
    d = dict(league="kbl", event_id="1", home="A", away="B", market=market, selection=sel, side=sel, line=line,
             odds=odds, p=p, book="sportybet")
    d["ev"] = p * odds - 1
    d.update(kw)
    return d


def test_max_ev_defaults_and_env(monkeypatch):
    assert gates.max_ev("live") == 0.10 and gates.max_ev("sim") == 0.12 and gates.max_ev("prematch") == 0.20
    monkeypatch.setenv("MAX_EV_LIVE", "0.3")
    assert gates.max_ev("live") == 0.3


def test_shrink_live_pulls_toward_book(monkeypatch):
    monkeypatch.setenv("LIVE_BOOK_BLEND", "0.5")
    over = c("over", 0.70, odds=1.9)
    under = c("under", 0.30, odds=1.9)
    out = gates.shrink_live([over, under])
    o = [x for x in out if x["selection"] == "over"][0]
    assert abs(o["p"] - 0.60) < 0.02  # (0.70 + 0.50)/2
    assert o["p"] < over["p"] and o["p_model"] == 0.70


def test_shrink_live_drops_unpaired():
    assert gates.shrink_live([c("over", 0.7)]) == []


def test_direction_and_blocks(monkeypatch):
    monkeypatch.setenv("LIVE_GAME_COOLDOWN_MIN", "45")
    monkeypatch.setenv("LIVE_MAX_PER_GAME", "3")
    now = datetime.utcnow()
    p = c("over", 0.65)
    assert gates.direction(p) == ("total", "over")
    recent = [dict(market="totals", selection="over", side="over", line=150.5, created_at=now - timedelta(minutes=5))]
    assert gates.live_blocked(p, recent, now)  # inside cooldown
    assert gates.live_blocked(c("under", 0.65), recent, now)  # opposite side
    assert not gates.live_blocked(p, [], now)


def test_old_alert_past_cooldown_same_direction_ok(monkeypatch):
    monkeypatch.setenv("LIVE_GAME_COOLDOWN_MIN", "45")
    now = datetime.utcnow()
    old = [dict(market="totals", selection="over", side="over", line=150.5, created_at=now - timedelta(minutes=90))]
    assert not gates.live_blocked(c("over", 0.65), old, now)
    assert gates.live_blocked(c("under", 0.65), old, now)  # opposite still blocked


def test_cap_per_game(monkeypatch):
    monkeypatch.setenv("LIVE_MAX_PER_GAME", "2")
    monkeypatch.setenv("LIVE_GAME_COOLDOWN_MIN", "0")
    now = datetime.utcnow()
    h = [dict(market="totals", selection="over", side="over", line=l, created_at=now - timedelta(minutes=100)) for l in (140.5, 145.5)]
    assert gates.live_blocked(c("over", 0.65), h, now)


def test_unknown_live_market_logged_once():
    import logging
    from core.bookmakers import sportybet as sb
    sb._seen_live_mkts.discard("99901")
    msgs = []

    class H(logging.Handler):
        def emit(self, r):
            msgs.append(r.getMessage())
    h, lg = H(level=logging.INFO), sb.logger
    old = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        m = {"id": 99901, "desc": "1st Quarter - Total", "specifier": "total=50.5", "status": 0,
             "outcomes": [{"id": "12", "desc": "Over", "odds": "1.9"}]}
        ev = {"period": "1", "matchStatus": "Q1"}
        sb._log_unknown_market(ev, m)
        sb._log_unknown_market(ev, m)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    assert sum("live market seen: id=99901" in x for x in msgs) == 1
