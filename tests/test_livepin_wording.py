from datetime import datetime, timedelta
from core.pinref import PinRef


def test_probe_with_no_game_in_play_does_not_claim_prices_were_dropped(monkeypatch):
    pr = PinRef.__new__(PinRef)
    pr.data = {"leagues": {}, "extras": {}}
    pr._live_raw = {}
    monkeypatch.setattr(PinRef, "live_fetch", lambda self, lg, force=False: ([], None))
    rows, notes = pr.live_probe(["acb"])
    assert rows == [] and notes and "no game in play" in notes[0] and "dropped" not in notes[0]
