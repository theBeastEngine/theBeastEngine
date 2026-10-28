"""Group B: ratings, client, cache, logger, fixtures fallback."""
import json
import logging
import types
import urllib.error
from datetime import datetime, timedelta, timezone

from core.bookmakers import sportybet as sb
from core.scores import sofascore as sf
from services import cache, client, ratings


# ---------- ratings ----------
def _tiers(monkeypatch, tmp_path, data):
    f = tmp_path / "t.json"
    f.write_text(json.dumps(data))
    monkeypatch.setattr(ratings, "_PATH", f)
    ratings.reload()


def test_ratings_accents_hyphens_and_none(monkeypatch, tmp_path):
    _tiers(monkeypatch, tmp_path, {"bsl": {"TOP": ["Fenerbah\u00e7e", "Anadolu-Efes"], "BOT": ["Bursaspor"]}})
    assert ratings.tier_of("bsl", "Fenerbahce") == "TOP"
    assert ratings.tier_of("bsl", "Anadolu Efes") == "TOP"
    assert ratings.tier_of("bsl", "Bursaspor") == "BOT"
    assert ratings.tier_of("bsl", "Unknown FC") == "MID"
    assert ratings.tier_of("bsl", None) == "MID"
    assert ratings.matchup_type("bsl", "Fenerbahce", "Bursaspor") == "TOP_BOT"
    ratings.reload()


def test_ratings_missing_file_warns_once(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ratings, "log", types.SimpleNamespace(warning=lambda *a, **k: seen.append(a)))
    monkeypatch.setattr(ratings, "_PATH", tmp_path / "nope.json")
    ratings.reload()
    assert ratings.tier_of("nba", "Boston Celtics") == "MID"
    assert ratings.tier_of("nba", "Miami Heat") == "MID"
    assert len(seen) == 1
    ratings.reload()


def test_ratings_both_formats(monkeypatch, tmp_path):
    _tiers(monkeypatch, tmp_path, {"nba": {"Boston Celtics": "top", "Utah Jazz": "BOT"}})
    assert ratings.tier_of("nba", "Boston Celtics") == "TOP" and ratings.tier_of("nba", "Utah Jazz") == "BOT"
    ratings.reload()


# ---------- client ----------
class _Resp:
    def __init__(self, body):
        self._b, self.headers = body, {"Content-Type": "application/json"}

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_client_wraps_connection_reset(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    n = []

    def boom(*a, **k):
        n.append(1)
        raise ConnectionResetError("reset by peer")
    monkeypatch.setattr(client.urllib.request, "urlopen", boom)
    try:
        client.get_json("https://x.test/a")
        assert False, "should raise"
    except client.HttpError as e:
        assert e.status == 0 and "reset" in str(e)
    assert len(n) == 3                                      # retried


def test_client_html_reply_is_http_error_not_valueerror(monkeypatch):
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *a, **k: _Resp(b"<html>blocked</html>"))
    try:
        client.get_json("https://x.test/a")
        assert False
    except client.HttpError as e:
        assert e.status == 200 and "not JSON" in e.body
    try:
        client.post_json("https://x.test/a", {"a": 1})
        assert False
    except client.HttpError as e:
        assert e.status == 200


def test_client_ok_and_http_status(monkeypatch):
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *a, **k: _Resp(b'{"a": 1}'))
    data, hdr = client.get_json("https://x.test/a")
    assert data == {"a": 1} and hdr["content-type"] == "application/json"

    def forbidden(*a, **k):
        raise urllib.error.HTTPError("https://x.test/a", 403, "Forbidden", {}, _Resp(b'{"error": "no"}'))
    monkeypatch.setattr(client.urllib.request, "urlopen", forbidden)
    try:
        client.get_json("https://x.test/a")
        assert False
    except client.HttpError as e:
        assert e.status == 403


# ---------- cache ----------
def test_cache_basic_and_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cache.time, "time", lambda: now[0])
    c = cache.TTLCache(ttl=10)
    c.set("a", 1)
    assert c.get("a") == 1
    now[0] += 11
    assert c.get("a") is None


def test_cache_is_bounded(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cache.time, "time", lambda: now[0])
    c = cache.TTLCache(ttl=1000, max_items=3)
    for i in range(6):
        now[0] += 1
        c.set(i, i)
    assert len(c._d) == 3 and c.get(5) == 5 and c.get(0) is None
    c2 = cache.TTLCache(ttl=5, max_items=3)
    for i in range(3):
        c2.set(i, i)
    now[0] += 10
    c2.set("new", 1)
    assert set(c2._d) == {"new"}                            # expired entries purged first


# ---------- logger ----------
def test_noisy_http_loggers_are_quiet():
    from services.logger import get_logger
    get_logger("x")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# ---------- fixtures fallback ----------
def _upcoming_payload():
    ts = int((datetime.now(timezone.utc) + timedelta(hours=5)).timestamp() * 1000)
    old = int((datetime.now(timezone.utc) - timedelta(hours=5)).timestamp() * 1000)
    far = int((datetime.now(timezone.utc) + timedelta(days=9)).timestamp() * 1000)
    ev = lambda eid, t, h="Boston Celtics", a="New York Knicks": {"eventId": eid, "homeTeamName": h, "awayTeamName": a, "estimateStartTime": t}
    return {"data": {"tournaments": [{"categoryName": "USA", "name": "NBA",
                                      "events": [ev("sr:match:1", ts), ev("sr:match:2", old), ev("sr:match:3", far), ev("sr:match:1", ts)]},
                                     {"categoryName": "Ukraine", "name": "BSKT CUP", "events": [ev("sr:match:9", ts)]}]}}


def test_fixtures_sync_shape_and_window(monkeypatch):
    async def fake_pages():
        return [_upcoming_payload()]
    monkeypatch.setattr(sb, "_pages", fake_pages)
    out = sb.fixtures_sync(2)
    assert [g["id"] for g in out] == ["sr:match:1"]
    g = out[0]
    assert g["league"] == "nba" and g["commence"].tzinfo is None and g["home"] == "Boston Celtics"


def test_sofascore_fixtures_fall_back_to_sportybet(monkeypatch):
    def forbidden(*a, **k):
        raise sf.HttpError(403, "Forbidden")
    monkeypatch.setattr(sf, "get_json", forbidden)
    monkeypatch.setattr(sb, "fixtures_sync", lambda days=2: [{"id": "fb"}])
    monkeypatch.setattr(sf, "FIXTURES_FALLBACK", True)
    assert sf.fixtures() == [{"id": "fb"}]
    monkeypatch.setattr(sf, "FIXTURES_FALLBACK", False)
    assert sf.fixtures() == []


def test_sofascore_fixtures_prefer_sofascore_when_it_works(monkeypatch):
    ts = int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp())
    ev = {"id": 5, "tournament": {"name": "NBA", "category": {"name": "USA"}}, "homeTeam": {"name": "A"},
          "awayTeam": {"name": "B"}, "status": {"type": "notstarted"}, "startTimestamp": ts}
    monkeypatch.setattr(sf, "get_json", lambda url, **k: ({"events": [ev]}, {}))
    monkeypatch.setattr(sb, "fixtures_sync", lambda days=2: [{"id": "fb"}])
    out = sf.fixtures(1)
    assert len(out) == 1 and out[0]["id"] == "5"
