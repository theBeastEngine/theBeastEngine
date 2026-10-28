import types
from datetime import datetime, timedelta

import pytest


def _http_error(status):
    from services.client import HttpError
    e = HttpError.__new__(HttpError)
    e.status = status
    return e


# ---- telegram: delivery status ----
def test_send_false_without_chat_id(monkeypatch):
    from core import telegram_notify as tn
    monkeypatch.setattr(tn, "settings", types.SimpleNamespace(TELEGRAM_CHAT_ID="", TELEGRAM_TOKEN="x"))
    assert tn.send("hi") is False


def test_send_false_on_http_error_true_on_ok(monkeypatch):
    from core import telegram_notify as tn

    def boom(method, **kw):
        raise _http_error(500)
    monkeypatch.setattr(tn, "api", boom)
    assert tn.send("hi", chat_id="1") is False
    monkeypatch.setattr(tn, "api", lambda method, **kw: {"ok": True})
    assert tn.send("hi", chat_id="1") is True


def test_format_pick_stake_is_whole_number():
    from core.telegram_notify import format_pick
    p = dict(mode="prematch", league="nba", home="A", away="B", market="h2h", selection="home", line=None,
             fair_odds=1.8, min_odds=1.85, book="sportybet", odds=1.9, ev=0.05, stake=5000.0)
    import re
    out = re.sub(r"</?b>", "", format_pick(p))
    assert "Stake: \u20a65,000" in out and "5,000." not in out


# ---- odds api: 429 vs 401 ----
def _api(monkeypatch, responses):
    from services import odds_api as oa
    calls = []

    def fake_get(url, params):
        calls.append(1)
        r = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(oa, "get_json", fake_get)
    monkeypatch.setattr(oa.time, "sleep", lambda s: None)
    return oa.OddsAPI(keys=["k" * 32]), calls


def test_429_is_retried_not_fatal(monkeypatch):
    api, calls = _api(monkeypatch, [_http_error(429), ([], {"x-requests-remaining": "10", "x-requests-last": "1"})])
    assert api.fetch("basketball_nba", markets=("h2h",)) == []
    assert len(calls) == 2 and not api.exhausted
    assert "k" * 32 not in str(api.masked_remaining())


def test_401_retires_key(monkeypatch):
    from services.odds_api import OutOfCredits
    api, _ = _api(monkeypatch, [_http_error(401)])
    with pytest.raises(OutOfCredits):
        api.fetch("basketball_nba", markets=("h2h",))
    assert api.exhausted


def test_zero_remaining_retires_key(monkeypatch):
    api, _ = _api(monkeypatch, [([], {"x-requests-remaining": "0", "x-requests-last": "1"})])
    api.fetch("basketball_nba", markets=("h2h",))
    assert api.exhausted


# ---- beast: one bad league must not kill the scan ----
def test_events_survive_one_bad_league(monkeypatch):
    import core.beast as cb

    def upcoming(lg):
        if lg == "bad":
            raise RuntimeError("boom")
        return [{"league": lg, "commence": (datetime.utcnow() + timedelta(hours=5)).isoformat()}]
    monkeypatch.setattr(cb.soft_feed, "upcoming", upcoming)
    monkeypatch.setattr(cb, "LEAGUE_KEYS", ["bad", "nba"])
    assert [e["league"] for e in cb.Beast()._events()] == ["nba"]


# ---- gold build: in-play games excluded ----
def test_upcoming_drops_started_games():
    from core.bookmakers.pinnacle import _upcoming
    now = datetime(2026, 10, 20, 12, 0)
    evs = [dict(commence=now - timedelta(hours=1)), dict(commence=now + timedelta(hours=3)), dict(commence=None)]
    assert len(_upcoming(evs, now=now)) == 2
