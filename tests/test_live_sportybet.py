"""SportyBet live feed (factsCenter/liveOrPrematchEvents): parsing, guards, cache, snapshot log, score routing."""
import asyncio
import json

from core.bookmakers import sportybet as sb
from core.bookmakers._base import parse_clock
from core.scores import sofascore as sf


def _mk(mid, outs, spec=None, status=0, avail="60:54", banned=False):
    m = {"id": str(mid), "status": status, "banned": banned, "availableScore": avail,
         "outcomes": [{"id": i, "desc": d, "odds": o, "isActive": 1} for i, d, o in outs]}
    if spec:
        m["specifier"] = spec
    return m


def _event(home="Boston Celtics", away="New York Knicks", cat="USA", period="3", status="Q3",
           played="35:25", rem="00:35", score="60:54", eid="sr:match:111", markets=None):
    return {"eventId": eid, "setScore": score, "period": period, "matchStatus": status, "playedSeconds": played,
            "remainingTimeInPeriod": rem, "homeTeamName": home, "awayTeamName": away,
            "estimateStartTime": 1791181800000, "sport": {"category": {"name": cat}},
            "markets": markets if markets is not None else [
                _mk(219, [("4", "Home", "1.35"), ("5", "Away", "2.95")], avail=score),
                _mk(223, [("1714", "Home", "1.90"), ("1715", "Away", "1.90")], "hcp=-5.5", avail=score),
                _mk(225, [("12", "Over 135.5", "1.88"), ("13", "Under 135.5", "1.92")], "total=135.5", avail=score)]}


def _payload(*events, name="NBA"):
    return {"bizCode": 10000, "message": "0#0", "data": [{"id": "t1", "name": name, "events": list(events)}]}


def test_parse_clock():
    assert parse_clock("29:25") == 1765 and parse_clock("00:35") == 35 and parse_clock("1:02:03") == 3723
    assert parse_clock("") is None and parse_clock("abc") is None and parse_clock(None) is None


def test_live_event_gets_game_clock_scores_and_rows():
    g = sb.parse_live(_payload(_event(), name="NBA"))[0]
    assert g["league"] == "nba" and g["period"] == 3 and (g["home_score"], g["away_score"]) == (60, 54)
    assert g["elapsed"] == 685.0 and g["remaining"] == 35          # 720 s NBA quarter, same as live_model
    keys = {(r["market"], r["side"]): r for r in g["rows"]}
    assert keys[("ML", "home")]["odds"] == 1.35 and keys[("ML", "away")]["odds"] == 2.95
    assert keys[("Spread", "home")]["line"] == -5.5 and keys[("Spread", "away")]["line"] == 5.5
    assert keys[("Total", "over")]["line"] == 135.5 and keys[("Total", "under")]["odds"] == 1.92


def test_nba_twelve_minute_quarters():
    g = sb.parse_live(_payload(_event(period="2", status="Q2", played="20:00", rem="04:00", score="50:48")))[0]
    assert g["period"] == 2 and g["elapsed"] == 480.0


def test_feed_period_length_must_match_the_model_format():
    # an "NBA" game whose feed plays 10-minute quarters (600 s) disagrees with live_model (720 s): skip it
    assert sb.parse_live(_payload(_event(played="29:25", rem="00:35"))) == []
    # Euroleague really is 4 x 10 min = the model default
    g = sb.parse_live(_payload(_event(cat="International", played="29:25", rem="00:35"), name="Euroleague"))[0]
    assert g["league"] == "euroleague" and g["elapsed"] == 565.0
    # NCAAB: 2 halves of 20 minutes
    g = sb.parse_live(_payload(_event(period="2", status="2nd half", played="30:00", rem="10:00"), name="NCAA"))[0]
    assert g["league"] == "ncaab" and g["period"] == 2 and not g["halftime"] and g["elapsed"] == 600.0


def test_suspended_banned_and_stale_markets_are_skipped():
    mk = [_mk(219, [("4", "Home", "1.35"), ("5", "Away", "2.95")], status=2),
          _mk(223, [("1714", "Home", "1.9"), ("1715", "Away", "1.9")], "hcp=-5.5", banned=True),
          _mk(225, [("12", "Over", "1.9"), ("13", "Under", "1.9")], "total=135.5", avail="58:54")]
    g = sb.parse_live(_payload(_event(markets=mk)))[0]
    assert g["rows"] == []


def test_sides_come_from_ids_not_position():
    mk = [_mk(219, [("5", "Away", "2.95"), ("4", "Home", "1.35")])]       # reversed order
    rows = sb.parse_live(_payload(_event(markets=mk)))[0]["rows"]
    assert {r["side"]: r["odds"] for r in rows} == {"home": 1.35, "away": 2.95}
    unknown = [_mk(219, [("77", "Team A", "1.35"), ("78", "Team B", "2.95")])]
    assert sb.parse_live(_payload(_event(markets=unknown)))[0]["rows"] == []


def test_wrong_leagues_and_bad_clock_are_dropped():
    assert sb.parse_live(_payload(_event(cat="Ukraine"), name="BSKT CUP")) == []
    assert sb.parse_live(_payload(_event(cat="Philippines"), name="NCAA, Seniors")) == []
    assert sb.parse_live(_payload(_event(played="", rem=""))) == []
    assert sb.parse_live(_payload(_event(period="5", status="OT"))) == []
    assert sb.parse_live({"bizCode": 10000, "data": {}}) == []


def test_second_half_is_not_a_break():
    for st in ("2nd half", "1st Half", "H2"):
        g = sb.parse_live(_payload(_event(period="2", status=st, played="30:00", rem="10:00"), name="NCAA"))[0]
        assert not g["halftime"] and g["period"] == 2
    for st in ("Halftime", "Half Time", "HT", "Break", "Pause"):
        g = sb.parse_live(_payload(_event(period="2", status=st, played="20:00", rem="00:00")))[0]
        assert g["halftime"] and g["period"] == 0


def test_halftime_is_flagged_and_not_tradable():
    g = sb.parse_live(_payload(_event(period="2", status="Halftime", played="24:00", rem="00:00")))[0]
    assert g["halftime"] and g["period"] == 0 and not sb.tradable(g)


def test_tradable_guards():
    base = dict(halftime=False, period=2, played=1200, remaining=300, league="nba", home_score=50, away_score=48)
    assert sb.tradable(base)
    assert not sb.tradable({**base, "played": 60})                                    # first minutes
    assert not sb.tradable({**base, "period": 4, "remaining": 90})                    # last 2 minutes
    assert not sb.tradable({**base, "period": 4, "remaining": 400, "home_score": 100, "away_score": 70})
    assert sb.tradable({**base, "period": 3, "home_score": 100, "away_score": 70})    # blowout rule is final period only
    assert not sb.tradable({**base, "league": "ncaab", "period": 2, "remaining": 100})  # NCAAB: 2 halves


def _patch_feed(monkeypatch, payload, calls):
    async def fake(url, **kw):
        calls.append(url)
        return payload
    monkeypatch.setattr(sb, "get_json", fake)
    monkeypatch.setattr(sb, "SNAPSHOT_LOG", False)
    sb._live_cache.update(t=0.0, payload=None)


def test_fetch_native_live_returns_only_the_requested_game_and_caches(monkeypatch):
    calls = []
    _patch_feed(monkeypatch, _payload(_event(), _event(home="Miami Heat", away="Orlando Magic", eid="sr:match:222")), calls)
    rows = asyncio.run(sb.fetch_native_live("nba", "Boston Celtics", "New York Knicks"))
    assert rows and all(r["home"] == "Boston Celtics" and r["live"] for r in rows)
    assert rows[0]["elapsed"] == 685.0 and rows[0]["period"] == 3
    assert asyncio.run(sb.fetch_native_live("nba", "Miami Heat", "Orlando Magic"))
    assert asyncio.run(sb.fetch_native_live("nba", "LA Lakers", "Utah Jazz")) == []
    assert len(calls) == 1                                  # one request served all three lookups
    sb._live_cache["t"] -= 100                              # cache expired -> refetch
    asyncio.run(sb.fetch_native_live("nba", "Miami Heat", "Orlando Magic"))
    assert len(calls) == 2


def test_untradable_game_gives_no_odds(monkeypatch):
    calls = []
    _patch_feed(monkeypatch, _payload(_event(period="4", status="Q4", played="46:30", rem="01:30", score="90:85",
                                             markets=[])), calls)
    assert asyncio.run(sb.fetch_native_live("nba", "Boston Celtics", "New York Knicks")) == []


def test_bad_live_payload_is_never_cached(monkeypatch):
    calls = []
    _patch_feed(monkeypatch, {"bizCode": 50000, "data": []}, calls)
    assert asyncio.run(sb.fetch_native_live("nba", "A", "B")) == []
    assert sb._live_cache["payload"] is None


def test_live_games_sync_shape(monkeypatch):
    calls = []
    _patch_feed(monkeypatch, _payload(_event()), calls)
    gs = sb.live_games_sync()
    assert len(gs) == 1 and "rows" not in gs[0]
    assert {"id", "league", "home", "away", "home_score", "away_score", "period", "halftime", "elapsed"} <= set(gs[0])


def test_snapshot_log_is_rate_limited(monkeypatch, tmp_path):
    monkeypatch.setenv("BEAST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sb, "SNAPSHOT_LOG", True)
    sb._last_snap.clear()
    games = sb.parse_live(_payload(_event()))
    sb._snapshot(games)
    sb._snapshot(games)
    files = list((tmp_path / "live_log").glob("*.jsonl"))
    lines = files[0].read_text().strip().splitlines()
    assert len(files) == 1 and len(lines) == 1 and json.loads(lines[0])["elapsed"] == 685.0


def test_sofascore_live_games_defaults_to_sportybet(monkeypatch):
    monkeypatch.setattr(sf, "LIVE_SOURCE", "sportybet")
    monkeypatch.setattr(sb, "live_games_sync", lambda *a, **k: [{"id": "x"}])
    assert sf.live_games() == [{"id": "x"}]

    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(sb, "live_games_sync", boom)
    assert sf.live_games() == []                            # no real clock -> no live games, never a wrong clock
    monkeypatch.setattr(sf, "LIVE_SOURCE", "sofascore")
    monkeypatch.setattr(sf, "_sofascore_live", lambda: [{"id": "old"}])
    assert sf.live_games() == [{"id": "old"}]
