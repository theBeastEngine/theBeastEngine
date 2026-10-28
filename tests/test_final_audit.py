"""Regression tests for the Dyip-Tropang live loss (7 Oct 2026)."""
from datetime import datetime, timedelta, timezone

from config.settings import PINNACLE
from core import live_scanner as ls
from core.live_model import frac_remaining, live_prob
from core.pinref import PinRef


# ---- totals must follow the pace the game is really playing ----
def test_live_total_uses_real_pace_dyip_case():
    # PBA Q4, 6:19 left, 76-100 (176 pts). Pregame total prior 190. Final total was 208.
    rem = frac_remaining("pba", 4, 720 - 379)
    p_over = live_prob("totals", "over", 193.5, rem, -24, 176, 0.0, 190.0, 12.0)
    assert 1 - p_over < 0.2  # Under 193.5 was sent as a 64.5% bet; it is a long shot


def test_live_total_unchanged_at_tipoff():
    # nothing played yet: the projection is still the pregame total
    p_over = live_prob("totals", "over", 220.0, 1.0, 0, 0, 0.0, 220.0, 12.0)
    assert abs(p_over - 0.5) < 1e-6


# ---- the alert must show score, period and clock ----
def _ev():
    c = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    return dict(event_id="s1", league="nba", home="LA Lakers", away="Boston Celtics", commence=c, markets={
        "h2h": [dict(book="sportybet", selection="home", line=None, odds=1.90),
                dict(book="sportybet", selection="away", line=None, odds=1.90)]})


def test_live_candidates_carry_score_period_and_clock(monkeypatch):
    monkeypatch.setattr(ls, "gold_index", lambda lg: {})
    monkeypatch.setattr(ls.soft_feed, "live_game", lambda lg, h, a: [_ev()])
    g = dict(league="nba", home="LA Lakers", away="Boston Celtics", halftime=False, period=4, elapsed=401.0,
             remaining=319, home_score=76, away_score=100)
    c = ls.LiveScanner().candidates(games=[g])[0]
    assert (c["period"], c["home_score"], c["away_score"], c["remaining_sec"]) == (4, 76, 100, 319)


def test_live_alert_text_shows_score_and_clock():
    from core.telegram_notify import format_pick
    p = dict(id=1, mode="live", league="pba", home="Dyip", away="Tropang 5G", period=4, remaining_sec=379,
             home_score=76, away_score=100, market="spreads", selection="home", line=9.5, ev=0.15, p=0.62,
             book="sportybet", odds=1.85, fair_odds=1.61, min_odds=1.67, stake=5000)
    import re
    txt = re.sub(r"</?b>", "", format_pick(p))
    assert "Q4 6:19 left" in txt and "Dyip 76 - 100 Tropang 5G" in txt


# ---- a Pinnacle refresh must not delete the lines of a game that is already in play ----
def _raw(gid, h, a, hours):
    t = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": gid, "home_team": h, "away_team": a, "commence_time": t, "bookmakers": [
        {"key": PINNACLE, "markets": [
            {"key": "spreads", "outcomes": [{"name": h, "price": 1.91, "point": -4.5},
                                            {"name": a, "price": 1.91, "point": 4.5}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": 1.90, "point": 220.5},
                                           {"name": "Under", "price": 1.92, "point": 220.5}]}]}]}


class _API:
    def __init__(self, raws):
        self.raws = raws

    def fetch(self, key, markets=None, books=None, regions="eu"):
        return self.raws


def test_refresh_keeps_in_play_game_for_live(tmp_path):
    ev = dict(event_id="x", league="nba", home="Los Angeles Lakers", away="Boston Celtics", commence=None)
    api = _API([_raw("g1", "Los Angeles Lakers", "Boston Celtics", 24)])
    ref = PinRef(api=api, path=str(tmp_path / "p.json"))
    ref.refresh("nba")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    ref.data["leagues"]["nba"]["events"][0]["commence"] = past  # tip-off was an hour ago
    api.raws = [_raw("g2", "Miami Heat", "New York Knicks", 30)]  # next refresh no longer lists g1
    assert ref.refresh("nba") == 1  # returns the number of UPCOMING games
    assert ref.match(ev, allow_started=True) is not None  # live can still see its Pinnacle lines
    assert ref.match(ev) is None  # prematch still refuses a started game
    assert ref.pregame_params(ev) == (4.5, 220.5)


def test_refresh_drops_games_older_than_the_live_window(tmp_path):
    ev = dict(event_id="x", league="nba", home="Los Angeles Lakers", away="Boston Celtics", commence=None)
    api = _API([_raw("g1", "Los Angeles Lakers", "Boston Celtics", 24)])
    ref = PinRef(api=api, path=str(tmp_path / "p.json"))
    ref.refresh("nba")
    old = (datetime.now(timezone.utc) - timedelta(hours=9)).replace(tzinfo=None).isoformat()
    ref.data["leagues"]["nba"]["events"][0]["commence"] = old
    api.raws = [_raw("g2", "Miami Heat", "New York Knicks", 30)]
    ref.refresh("nba")
    assert ref.match(ev, allow_started=True) is None


# ---- live: 3 best picks = 3 different games, and games without a real Pinnacle price never take a slot ----
def test_scan_once_gates_on_real_pinnacle_before_ranking(monkeypatch):
    class Pin:
        def pregame_params(self, ev):
            return (1.5, 200.0) if ev["league"] == "nba" else None

    sc = ls.LiveScanner(pinref=Pin())
    cands = [dict(league="pba", home="Dyip", away="Tropang", ev=0.18),
             dict(league="nba", home="A", away="B", ev=0.06)]
    monkeypatch.setattr(sc, "candidates", lambda games=None: cands)
    monkeypatch.setattr(ls, "finalize", lambda c, mode, **kw: c)
    assert [c["league"] for c in sc.scan_once()] == ["nba"]


def test_finalize_live_is_best_pick_per_game_then_top_n(monkeypatch):
    from core.filter import Ctx, finalize
    from storage import repository as repo
    monkeypatch.setattr(repo, "recently_sent", lambda *a, **k: False)
    monkeypatch.setattr(repo, "bets_today_on_book", lambda *a, **k: 0)
    monkeypatch.setattr(repo, "live_history", lambda *a, **k: [])
    monkeypatch.setenv("MAX_EV_LIVE", "0.5")  # this test is about ranking, not the EV cap

    def c(eid, line, p):
        return dict(league="nba", event_id=eid, home="H" + eid, away="A" + eid, market="spreads", selection="home",
                    line=line, odds=1.85, p=p, book="sportybet", book_margin=0.05)

    cands = [c("1", 9.5, 0.62), c("1", 10.5, 0.63), c("1", 16.5, 0.64),   # one game, three lines
             c("2", 3.5, 0.60), c("3", 2.5, 0.60), c("4", 1.5, 0.59)]
    ctx = Ctx()
    assert finalize([dict(c) for c in cands], "live", bankroll=100000, ctx=ctx) == []  # 1st sighting: not confirmed yet
    out = finalize([dict(c) for c in cands], "live", bankroll=100000, ctx=ctx)           # 2nd scan: confirmed
    assert len(out) == 3 and len({o["event_id"] for o in out}) == 3
    assert [o["line"] for o in out if o["event_id"] == "1"] == [16.5]  # that game's best EV
