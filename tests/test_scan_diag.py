from datetime import datetime, timedelta, timezone

from core import beast as B


def _t(h):
    return (datetime.now(timezone.utc) + timedelta(hours=h)).replace(tzinfo=None).isoformat()


class _Pin:
    data = {"leagues": {"euroleague": {"events": [
        {"home": "Crvena Zvezda", "away": "Dubai Basketball", "commence": _t(5)},
        {"home": "Far Away", "away": "Game", "commence": _t(200)}]}}}


def _beast():
    b = B.Beast.__new__(B.Beast)
    b.pinref = _Pin()
    return b


def test_pin_near_lists_only_games_around_the_same_time():
    ev = {"league": "euroleague", "home": "KK Crvena zvezda Belgrade", "away": "Dubai Basketball", "commence": _t(6)}
    assert _beast()._pin_near(ev) == ["Crvena Zvezda vs Dubai Basketball"]


def test_pin_near_never_raises():
    b = B.Beast.__new__(B.Beast)
    b.pinref = object()  # no .data
    assert b._pin_near({"league": "nba", "commence": _t(1)}) == []
    assert b._pin_spans() == "unavailable"
