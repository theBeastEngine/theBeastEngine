from datetime import timedelta

from core import livepin
from core.pinref import PinRef, _now


class FakeApi:
    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def fetch(self, key, books=None):
        self.calls += 1
        return self.raw


def _raw(start, over, under):
    return [dict(id="g1", sport_key="basketball_nba", commence_time=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 home_team="Boston Celtics", away_team="New York Knicks",
                 bookmakers=[dict(key="pinnacle", title="Pinnacle", markets=[
                     dict(key="totals", outcomes=[dict(name="Over", price=over, point=210.5),
                                                  dict(name="Under", price=under, point=210.5)])])])]


def test_odds_api_live_fetch_keeps_games_in_play_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("PIN_REF_PATH", str(tmp_path / "p.json"))
    api = FakeApi(_raw(_now() - timedelta(minutes=50), 1.55, 2.50))
    pr = PinRef(api=api, path=str(tmp_path / "p.json"), daily_cap=100)
    events, why = pr.live_fetch("nba")
    assert why is None and events and events[0]["home"]
    pr.live_fetch("nba")
    assert api.calls == 1                      # cached for LIVE_PIN_MIN_SEC
    assert pr.spent_today() > 0


def test_odds_api_live_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_ODDSAPI_DAILY_CAP", "0")
    api = FakeApi(_raw(_now() - timedelta(minutes=50), 1.55, 2.50))
    pr = PinRef(api=api, path=str(tmp_path / "p.json"), daily_cap=100)
    events, why = pr.live_fetch("nba")
    assert events is None and "cap" in why and api.calls == 0


def test_model_only_pick_gets_caution_cut(monkeypatch):
    p = dict(market="totals", selection="over", line=150.5, odds=1.75, p=0.70, ev=0.225, league="pba",
             home="A", away="B", mode="live", fair_odds=1.43)
    act, q, _ = livepin.verify(p, None, None, md="soft", min_ev=0.04, min_p=0.6, max_ev=0.5)
    assert act == "keep" and abs(q["p"] - 0.67) < 1e-9 and q["pin_live"] is False
    weak = dict(p, p=0.62, odds=1.70)
    assert livepin.verify(weak, None, None, md="soft", min_ev=0.04, min_p=0.6, max_ev=0.5)[0] == "drop"
    monkeypatch.setenv("LIVE_MODEL_ONLY_HAIRCUT", "0")
    assert livepin.verify(weak, None, None, md="soft", min_ev=0.04, min_p=0.6, max_ev=0.5)[0] == "keep"


def test_probe_reports_what_each_source_returned(tmp_path):
    api = FakeApi(_raw(_now() + timedelta(hours=3), 1.9, 1.9))   # nothing started yet
    pr = PinRef(api=api, path=str(tmp_path / "p.json"), daily_cap=100)
    rows, notes = pr.live_probe(["nba"])
    assert rows == [] and any("Odds API: 1 game(s) listed, 0 already started" in n for n in notes)
