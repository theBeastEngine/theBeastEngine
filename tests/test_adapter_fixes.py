"""Adapter fixes: league rules, block-page handling, capture file, SportyBet cache, soft_feed logging."""
import asyncio
import json
import sys
import types

from core.bookmakers import _base, sportybet, soft_feed
from core.bookmakers._base import league_matches, infer_league


# ---------- league rules ----------
def test_lnb_ignores_chile_and_argentina():
    assert not league_matches("Chile LNB", "lnb")
    assert not league_matches("Argentina Liga Nacional LNB", "lnb")
    assert infer_league("Chile LNB") is None


def test_lnb_keeps_french_top_flight():
    assert league_matches("France LNB Pro A", "lnb")
    assert league_matches("France Betclic Elite", "lnb")
    assert not league_matches("France LNB Pro B", "lnb")


def test_acb_ignores_womens_liga():
    assert league_matches("Spain Liga Endesa", "acb")
    assert not league_matches("Spain Liga Femenina", "acb")


def test_nba_rules_unchanged():
    assert league_matches("USA NBA", "nba")
    assert not league_matches("USA NBA G League", "nba")
    assert infer_league("USA NBA G League") == "g_league"
    assert not league_matches("USA WNBA", "nba")


# ---------- get_json ----------
class _Resp:
    def __init__(self, status=200, url="https://x.test/api", payload=None, bad_json=False):
        self.status_code, self.url, self.payload, self.bad_json = status, url, payload, bad_json
        self.headers = {"content-type": "text/html"}

    def json(self):
        if self.bad_json:
            raise ValueError("not json")
        return self.payload


def _fake_httpx(resp, calls):
    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            calls.append(url)
            return resp
    return types.SimpleNamespace(AsyncClient=Client)


def _fake_fetch(reply, calls):
    def fake(url, params, hdr, timeout):
        calls.append(url)
        return reply
    return fake


def test_get_json_stops_at_block_page(monkeypatch):
    calls = []
    monkeypatch.setattr(_base, "_fetch_sync",
                        _fake_fetch((200, "https://1xbet.ng/en/block", "text/html", "<html>"), calls))
    assert asyncio.run(_base.get_json("https://1xbet.ng/LineFeed")) is None
    assert len(calls) == 1


def test_get_json_html_200_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(_base, "_fetch_sync",
                        _fake_fetch((200, "https://x.test/api", "text/html", "<html>"), calls))
    assert asyncio.run(_base.get_json("https://x.test/api")) is None
    assert len(calls) == 1


def test_get_json_ok(monkeypatch):
    calls = []
    monkeypatch.setattr(_base, "_fetch_sync",
                        _fake_fetch((200, "https://x.test/api", "application/json", '{"a": 1}'), calls))
    assert asyncio.run(_base.get_json("https://x.test/api")) == {"a": 1}


def test_get_json_403_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(_base, "_fetch_sync", _fake_fetch((403, "https://x.test/api", "", ""), calls))
    assert asyncio.run(_base.get_json("https://x.test/api")) is None
    assert len(calls) == 1


# ---------- save_capture ----------
def test_capture_is_complete_json_and_written_once(monkeypatch, tmp_path):
    monkeypatch.setenv("BEAST_DATA_DIR", str(tmp_path))
    _base._captured.discard("zz")
    big = {"rows": ["x" * 100] * 5000}           # ~500k chars, over the old 300k cut
    _base.save_capture("zz", big)
    _base.save_capture("zz", {"second": True})   # must not overwrite
    got = json.loads((tmp_path / "captures" / "zz.json").read_text())
    assert got == big


# ---------- SportyBet ----------
def _payload():
    ev = {"eventId": "sr:match:1", "homeTeamName": "Boston Celtics", "awayTeamName": "New York Knicks",
          "estimateStartTime": 1790000000000, "markets": [
              {"id": 219, "outcomes": [{"odds": "1.50"}, {"odds": "2.60"}]},
              {"id": 223, "specifier": "hcp=-5.5", "outcomes": [{"odds": "1.90"}, {"odds": "1.90"}]},
              {"id": 225, "specifier": "total=220.5",
               "outcomes": [{"desc": "Over 220.5", "odds": "1.88"}, {"desc": "Under 220.5", "odds": "1.92"}]}]}
    return {"data": {"tournaments": [{"categoryName": "USA", "name": "NBA", "events": [ev]}]}}


def test_sportybet_parse_gives_six_rows():
    rows = sportybet.parse_events(_payload(), "nba")
    assert len(rows) == 6
    sp = {r["side"]: r["line"] for r in rows if r["market"] == "Spread"}
    assert sp == {"home": -5.5, "away": 5.5}
    assert all(r["league"] == "nba" for r in rows)


def test_sportybet_one_fetch_serves_all_leagues(monkeypatch):
    calls = []

    async def fake(url, **kw):
        calls.append(kw["params"]["pageNum"])
        return _payload() if kw["params"]["pageNum"] == 1 else {}
    monkeypatch.setattr(sportybet, "get_json", fake)
    sportybet._cache.update(t=0.0, pages=None)
    first = asyncio.run(sportybet.fetch_odds("nba"))
    n_first = len(calls)
    for lg in ("euroleague", "acb", "lnb", "bbl", "g_league"):
        assert asyncio.run(sportybet.fetch_odds(lg)) == []
    assert len(calls) == n_first and len(first) == 6
    assert len(asyncio.run(sportybet.fetch_odds("nba"))) == 6      # still cached, same rows
    sportybet._cache.update(t=0.0, pages=None)


def test_sportybet_empty_result_not_cached(monkeypatch):
    async def fake(url, **kw):
        return None
    monkeypatch.setattr(sportybet, "get_json", fake)
    sportybet._cache.update(t=0.0, pages=None)
    assert asyncio.run(sportybet.fetch_odds("nba")) == []
    assert sportybet._cache["pages"] is None


# ---------- soft_feed ----------
def test_soft_feed_logs_adapter_failure(monkeypatch):
    seen = []
    monkeypatch.setattr(soft_feed, "log", types.SimpleNamespace(warning=lambda *a, **k: seen.append(a)))
    saved = list(soft_feed._ADAPTERS)
    soft_feed._ADAPTERS[:] = []

    class Boom(soft_feed.SoftAdapter):
        book = "boom"

        def upcoming(self, league):
            raise RuntimeError("feed down")
    soft_feed.register(Boom())
    try:
        assert soft_feed.upcoming("nba") == []
    finally:
        soft_feed._ADAPTERS[:] = saved
    assert seen and "boom" in str(seen[0]) and "feed down" in str(seen[0])


def test_rows_to_events_flips_away_spread_to_home_line():
    rows = sportybet.parse_events(_payload(), "nba")
    ev = soft_feed.rows_to_events(rows, "nba")[0]
    assert {o["line"] for o in ev["markets"]["spreads"]} == {-5.5}
    assert ev["markets"]["h2h"][0]["selection"] == "home"
