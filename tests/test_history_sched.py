import json
from datetime import datetime, timedelta

from core import pinhist, teamsched

NOW = datetime(2026, 10, 20, 12, 0)


def _ev(eid, home, away, start, **kw):
    return dict(event_id=eid, home=home, away=away, commence=start.isoformat(), **kw)


class Store:
    def __init__(self):
        self.text = None
        self.saves = 0

    def load(self):
        return self.text

    def save(self, t):
        self.text = t
        self.saves += 1


def _sched(store):
    return teamsched.TeamSchedule(store.load, store.save, now=lambda: NOW)


def test_record_and_starts():
    st = Store()
    ts = _sched(st)
    assert ts.record("nba", [_ev("1", "Kings", "Lakers", NOW + timedelta(hours=5))])
    assert ts.starts("nba", "Kings") == [NOW + timedelta(hours=5)]
    assert ts.starts("nba", "Nobody") == []
    assert not ts.record("nba", [_ev("1", "Kings", "Lakers", NOW + timedelta(hours=5))])  # nothing new
    assert st.saves == 1


def test_survives_restart_and_reschedule():
    st = Store()
    _sched(st).record("nba", [_ev("1", "Kings", "Lakers", NOW + timedelta(hours=5))])
    ts2 = _sched(st)  # new object reads what was saved
    ts2.record("nba", [_ev("1", "Kings", "Lakers", NOW + timedelta(hours=30))])  # game moved
    assert ts2.starts("nba", "Lakers") == [NOW + timedelta(hours=30)]


def test_cancelled_future_game_is_forgotten_but_played_game_stays():
    ts = _sched(Store())
    played = _ev("p", "Kings", "Suns", NOW - timedelta(hours=20))
    future = _ev("f", "Kings", "Lakers", NOW + timedelta(hours=20))
    ts.record("nba", [played, future])
    ts.record("nba", [_ev("x", "Heat", "Nets", NOW + timedelta(hours=9))])  # full refresh without p and f
    assert ts.starts("nba", "Kings") == [NOW - timedelta(hours=20)]


def test_old_games_pruned_and_scopes_independent():
    ts = _sched(Store())
    ts.record("nba", [_ev("old", "Kings", "Suns", NOW - timedelta(days=9))])
    assert ts.starts("nba", "Kings") == []
    ts.record("nba", [_ev("a", "Kings", "Suns", NOW + timedelta(hours=8))], scope="main")
    ts.record("nba", [_ev("b", "Kings", "Heat", NOW + timedelta(hours=50))], scope="extra")  # does not drop "a"
    assert len(ts.starts("nba", "Kings")) == 2


def test_bad_events_ignored():
    ts = _sched(Store())
    ts.record("nba", [{"home": "A", "away": "B"}, {"event_id": "z", "home": "", "away": "B", "commence": NOW.isoformat()}])
    assert ts.starts("nba", "A") == []


def _fair(ev):
    return ev["_fair"]


def test_snapshot_picks_main_lines():
    ev = {"_fair": {("h2h", None): {"home": 0.61, "away": 0.39},
                    ("spreads", -4.5): {"home": 0.50, "away": 0.50},
                    ("spreads", -9.5): {"home": 0.75, "away": 0.25},
                    ("totals", 220.5): {"over": 0.49, "under": 0.51},
                    ("totals", 230.5): {"over": 0.80, "under": 0.20}}}
    s = pinhist.snapshot(ev, _fair)
    assert s == {"h2h": 0.61, "spread": [-4.5, 0.5], "total": [220.5, 0.49]}
    assert pinhist.snapshot({"_fair": {}}, _fair) is None


def test_history_logs_only_changes():
    rows = []
    h = pinhist.PriceHistory(rows.extend, now=lambda: NOW, fair_fn=_fair)
    ev = _ev("1", "Kings", "Lakers", NOW + timedelta(hours=3), _fair={("h2h", None): {"home": 0.6, "away": 0.4}})
    assert h.record("nba", [ev]) == 1
    assert h.record("nba", [ev]) == 0
    ev2 = dict(ev, _fair={("h2h", None): {"home": 0.62, "away": 0.38}})
    assert h.record("nba", [ev2]) == 1
    assert len(rows) == 2 and json.loads(rows[1]["snap"]) == {"h2h": 0.62}
    assert rows[0]["commence"] == NOW + timedelta(hours=3)


def _row(ts, snap, home="Kings", away="Lakers", start=None):
    return dict(ts=ts, home=home, away=away, commence=start or NOW + timedelta(hours=3), snap=json.dumps(snap))


def _same(a, b):
    return a == b


def test_closing_prob():
    rows = [_row(NOW, {"h2h": 0.60, "spread": [-4.5, 0.5], "total": [220.5, 0.48]}),
            _row(NOW + timedelta(hours=2), {"h2h": 0.64, "spread": [-5.5, 0.52], "total": [220.5, 0.45]}),
            _row(NOW + timedelta(hours=4), {"h2h": 0.99}),  # after tip-off: ignored
            _row(NOW, {"h2h": 0.10}, home="Heat", away="Nets")]
    pick = dict(home="Kings", away="Lakers", commence=NOW + timedelta(hours=3), market="h2h", selection="home", line=None)
    assert pinhist.closing_prob(pick, rows, _same) == 0.64
    assert abs(pinhist.closing_prob(dict(pick, selection="away"), rows, _same) - 0.36) < 1e-9
    assert pinhist.closing_prob(dict(pick, market="spreads", selection="home", line=-5.5), rows, _same) == 0.52
    assert pinhist.closing_prob(dict(pick, market="spreads", selection="home", line=-4.5), rows, _same) is None  # line moved
    assert abs(pinhist.closing_prob(dict(pick, market="totals", selection="under", line=220.5), rows, _same) - 0.55) < 1e-9
    assert pinhist.closing_prob(dict(pick, market="h2h_q1"), rows, _same) is None
    assert pinhist.closing_prob(dict(pick, home="Zzz"), rows, _same) is None
