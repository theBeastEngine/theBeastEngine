from datetime import datetime, timedelta, timezone

from config.settings import PINNACLE
from core import live_scanner as ls
from core.pinref import PinRef
from core.simulation import candidates_sim


def _iso(h):
    return (datetime.now(timezone.utc) + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _raw(hours=24):
    h, a = "Los Angeles Lakers", "Boston Celtics"
    return {"id": "g1", "home_team": h, "away_team": a, "commence_time": _iso(hours), "bookmakers": [
        {"key": PINNACLE, "markets": [
            {"key": "spreads", "outcomes": [{"name": h, "price": 1.91, "point": -4.5},
                                            {"name": a, "price": 1.91, "point": 4.5}]},
            {"key": "spreads", "outcomes": [{"name": h, "price": 1.60, "point": -6.5},
                                            {"name": a, "price": 2.40, "point": 6.5}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": 1.90, "point": 220.5},
                                           {"name": "Under", "price": 1.92, "point": 220.5}]}]}]}


class FakeAPI:
    def fetch(self, key, markets=None, books=None, regions="eu"):
        return [_raw()]


def _ev(hours=24):
    c = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=hours)
    return dict(event_id="s1", league="nba", home="LA Lakers", away="Boston Celtics", commence=c, markets={
        "h2h": [dict(book="sportybet", selection="home", line=None, odds=1.90),
                dict(book="sportybet", selection="away", line=None, odds=1.90)]})


def test_pregame_params_use_main_line_and_work_after_tipoff(tmp_path):
    ref = PinRef(api=FakeAPI(), path=str(tmp_path / "p.json"))
    ref.refresh("nba")
    assert ref.pregame_params(_ev()) == (4.5, 220.5)  # -4.5 is the pair closest to even, home margin +4.5
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    ref.data["leagues"]["nba"]["events"][0]["commence"] = past
    assert ref.rows_for(_ev(-1)) == []  # prematch matching refuses a started game
    assert ref.pregame_params(_ev(-1)) == (4.5, 220.5)  # live matching still sees it


def _p_home(cands):
    return next(c["p"] for c in cands if c["market"] == "h2h" and c["selection"] == "home")


def test_sim_params_override_tier_priors():
    strong = _p_home(candidates_sim(_ev(), {}, n=400, seed=1, params=(8.0, 220.5)))
    weak = _p_home(candidates_sim(_ev(), {}, n=400, seed=1, params=(-8.0, 220.5)))
    assert strong > 0.6 > 0.4 > weak


def _game(home="LA Lakers", away="Boston Celtics"):
    return dict(league="nba", home=home, away=away, halftime=False, period=2, elapsed=300,
                home_score=50, away_score=48)


def test_live_scanner_queries_gold_once_per_league_even_if_empty(monkeypatch):
    calls = []
    monkeypatch.setattr(ls, "gold_index", lambda lg: calls.append(lg) or {})
    monkeypatch.setattr(ls.soft_feed, "live_game", lambda lg, h, a: [_ev()])
    out = ls.LiveScanner().candidates(games=[_game(), _game()])
    assert out and calls == ["nba"]


def test_live_scanner_uses_pinnacle_lines(monkeypatch):
    monkeypatch.setattr(ls, "gold_index", lambda lg: {})
    monkeypatch.setattr(ls.soft_feed, "live_game", lambda lg, h, a: [_ev()])

    class Stub:
        def pregame_params(self, ev):
            return 25.0, 230.0

    base = _p_home(ls.LiveScanner().candidates(games=[_game()]))
    pin = _p_home(ls.LiveScanner(pinref=Stub()).candidates(games=[_game()]))
    assert pin > base
