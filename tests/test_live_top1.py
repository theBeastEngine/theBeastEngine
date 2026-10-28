from core import beast as B

def _cls():
    return next(v for v in vars(B).values() if isinstance(v, type) and hasattr(v, "run_live"))

class _Scan:
    def scan_once(self):
        return [{"ev": 0.05, "league": "nba", "home": "A", "away": "B"}, {"ev": 0.09, "league": "nba", "home": "C", "away": "D"},
                {"ev": 0.07, "league": "nba", "home": "E", "away": "F"}]   # three different games

class _Pin:
    # live picks need real Pinnacle lines of the same game: pretend every game has them
    def pregame_params(self, ev):
        return (1.5, 200.0)

class _Fake:
    live_scanner = _Scan()
    pinref = _Pin()
    def _persist(self, picks):
        return picks

def test_scheduled_live_keeps_all_three(monkeypatch):
    monkeypatch.setenv("STANCE_LOCK", "0")   # the stance gate has its own tests (tests/test_build_oct13.py)
    assert len(_cls().run_live(_Fake())) == 3

def test_telegram_live_sends_only_best(monkeypatch):
    monkeypatch.setenv("STANCE_LOCK", "0")
    out = _cls().run_live(_Fake(), top_n=1)
    assert len(out) == 1 and out[0]["ev"] == 0.09
