from datetime import timedelta

import pytest

from core import beast as B
from core.pinref import _now


def _iso(delta):
    return (_now() + delta).isoformat()


def _bare():
    return B.Beast.__new__(B.Beast)  # no network, no DB


def test_within_horizon(monkeypatch):
    monkeypatch.delenv("DAYS_AHEAD", raising=False)
    assert B.within_horizon(_iso(timedelta(hours=5)))
    assert not B.within_horizon(_iso(timedelta(days=30)))
    assert not B.within_horizon(_iso(-timedelta(hours=1)))
    assert not B.within_horizon(None)
    assert not B.within_horizon("garbage")
    monkeypatch.setenv("DAYS_AHEAD", "40")
    assert B.within_horizon(_iso(timedelta(days=30)))


def test_events_only_inside_horizon(monkeypatch):
    monkeypatch.delenv("DAYS_AHEAD", raising=False)
    soon = {"league": "nba", "home": "A", "away": "B", "event_id": "1", "commence": _iso(timedelta(hours=10))}
    far = {"league": "nba", "home": "C", "away": "D", "event_id": "2", "commence": _iso(timedelta(days=60))}
    monkeypatch.setattr(B, "LEAGUE_KEYS", ["nba"])
    monkeypatch.setattr(B.soft_feed, "upcoming", lambda lg: [soon, far])
    assert [e["event_id"] for e in _bare()._events()] == ["1"]


def _sim_beast(monkeypatch, params):
    class Pin:
        def pregame_params(self, ev):
            return params
    b = _bare()
    b.pinref = Pin()
    ev = {"league": "nba", "home": "A", "away": "B", "event_id": "1", "commence": _iso(timedelta(hours=3))}
    b._events = lambda: iter([ev])
    b._persist = lambda picks: picks
    monkeypatch.setattr(B, "candidates_sim", lambda e, idx, n=None, params=None: [{"event_id": e["event_id"]}])
    monkeypatch.setattr(B, "finalize", lambda c, mode: c)
    monkeypatch.setattr(B, "_idx", lambda cache, lg: {})
    return b


def test_sim_needs_real_pinnacle_lines(monkeypatch):
    monkeypatch.delenv("REQUIRE_REAL_PIN", raising=False)
    assert _sim_beast(monkeypatch, None).run_sim() == []
    assert len(_sim_beast(monkeypatch, (3.5, 220.0)).run_sim()) == 1


def test_sim_real_pin_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("REQUIRE_REAL_PIN", "0")
    assert len(_sim_beast(monkeypatch, None).run_sim()) == 1


def test_live_needs_real_pinnacle_lines(monkeypatch):
    monkeypatch.delenv("REQUIRE_REAL_PIN", raising=False)
    monkeypatch.setenv("LIVE_ENABLED", "1")
    picks = [
        {"league": "nba", "home": "A", "away": "B", "ev": 0.10},
        {"league": "pba", "home": "Dyip", "away": "X", "ev": 0.15},
    ]

    class Pin:
        def pregame_params(self, ev):
            return (1.5, 200.0) if ev["league"] == "nba" else None

    class Scan:
        def scan_once(self):
            return list(picks)

    b = _bare()
    b.pinref, b.live_scanner = Pin(), Scan()
    b._persist = lambda p: p
    out = b.run_live()
    assert [p["league"] for p in out] == ["nba"]
    assert b.run_live(top_n=0) == []


def test_persist_drops_picks_below_min_ev():
    b = _bare()
    b.notify = None
    assert b._persist([{"ev": 0.01}, {"ev": 0.039}, {"ev": None}]) == []


def _live_beast(picks):
    class Pin:
        def pregame_params(self, ev):
            return (1.5, 200.0)

    class Scan:
        def scan_once(self):
            return list(picks)

    b = _bare()
    b.pinref, b.live_scanner = Pin(), Scan()
    b._persist = lambda p: p
    return b


def test_live_is_on_by_default_with_emergency_off(monkeypatch):
    monkeypatch.delenv("LIVE_ENABLED", raising=False)
    picks = [{"league": "nba", "home": "A", "away": "B", "ev": 0.10}]
    assert len(_live_beast(picks).run_live()) == 1
    monkeypatch.setenv("LIVE_ENABLED", "0")
    assert _live_beast(picks).run_live() == []


def test_live_one_pick_per_game(monkeypatch):
    monkeypatch.setenv("LIVE_ENABLED", "1")
    picks = [
        {"league": "nba", "home": "A", "away": "B", "ev": 0.05, "line": 9.5},
        {"league": "nba", "home": "A", "away": "B", "ev": 0.12, "line": 10.5},
        {"league": "nba", "home": "A", "away": "B", "ev": 0.08, "line": 16.5},
        {"league": "nba", "home": "C", "away": "D", "ev": 0.06, "line": 3.5},
    ]
    out = _live_beast(picks).run_live()
    assert len(out) == 2
    assert [p["line"] for p in out if p["home"] == "A"] == [10.5]
