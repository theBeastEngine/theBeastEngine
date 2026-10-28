# core/bookmakers/sportybet.py
"""SportyBet (Sportradar-style). Basketball market ids: 219 winner, 223 handicap, 225 total (all incl. OT)."""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from core.bookmakers._base import (build_rows, fnum, get_json, log_shape_once, logger, most_balanced,
                                   parse_clock, same_team, save_capture, to_iso, wanted)

BOOK = "sportybet"
URL = os.getenv("SPORTYBET_URL", "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents")
# 219/223/225 full game; 236 quarter total, 303 quarter handicap (specifier quarternr=1..4); 68 / 66 first-half total /
# handicap; 90 / 88 second-half total / handicap
MARKET_IDS = os.getenv("SPORTYBET_MARKET_IDS", "219,223,225,236,303,68,66,88,90")
_PERIOD_MARKETS = {"236": ("total", None), "303": ("spread", None), "68": ("total", "h1"), "66": ("spread", "h1"),
                   "90": ("total", "h2"), "88": ("spread", "h2")}
_seen_period_names = set()


def _is_ot(m: Dict) -> bool:
    """SportyBet writes '(incl. overtime)' into the name of a market that counts overtime."""
    return "overtime" in ("%s %s" % (m.get("name", ""), m.get("desc", ""))).lower()


def _period_key(per: str, m: Dict) -> Optional[str]:
    """Only Q4 and the 2nd half can include overtime. Their code gets an 'ot' suffix when the book says so, so a
    handicap is only ever compared with Pinnacle's handicap of the same kind (which includes overtime), and a
    regulation total only with Pinnacle's regulation total."""
    if per in ("q4", "h2"):
        return per + "ot" if _is_ot(m) else per
    return per
PAGES = int(os.getenv("SPORTYBET_PAGES", "3"))
CACHE_S = float(os.getenv("SPORTYBET_CACHE_S", "90"))
HEADERS = {"operid": "2", "clientid": "web", "platform": "web", "Referer": "https://www.sportybet.com/ng/"}

# CLAUDE FIX: the feed is the same for every league, but fetch_odds(league) is called once per league
# (14 per sweep) and each call made PAGES requests. One short-lived cache turns that into PAGES requests per sweep.
_cache: Dict = {"t": 0.0, "pages": None}


def _spec(s) -> Dict[str, str]:
    out = {}
    for part in re.split(r"[&|]", str(s or "")):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _odds(o: Dict) -> Optional[float]:
    return None if o.get("isActive") in (0, False) else fnum(o.get("odds"))


def _two_sides(outs, kind, hcp=None):
    """{side: odds} for a two-way period market, sides read by id / description (never by position).
    A handicap whose HOME outcome names a different line than the specifier (sign convention differs) is dropped."""
    res = {}
    for o in outs:
        sd, px = _side(o), _odds(o)
        if sd and px:
            res[sd] = px
            if kind == "spread" and sd == "home" and hcp is not None:
                m = re.search(r"\(([+-]?\d+(?:\.\d+)?)\)", str(o.get("desc", "")))
                if m and abs(float(m.group(1)) - hcp) > 1e-9:
                    return {}
    keys = ("home", "away") if kind == "spread" else ("over", "under")
    return res if all(k in res for k in keys) else {}


def _period_rows(ev) -> Dict:
    """{'q1': {'spread': (home_line, h, a), 'total': (line, over, under)}, 'h1': {...}} - main (most balanced) line each."""
    cand: Dict = {}
    for m in ev.get("markets") or []:
        spec_info = _PERIOD_MARKETS.get(str(m.get("id")))
        if not spec_info:
            continue
        kind, fixed = spec_info
        sp = _spec(m.get("specifier"))
        per = fixed or ("q" + sp["quarternr"] if sp.get("quarternr", "").isdigit() and 1 <= int(sp["quarternr"]) <= 4 else None)
        if not per:
            continue
        tag = (str(m.get("id")), str(m.get("name") or m.get("desc") or "")[:60])
        if tag not in _seen_period_names and len(_seen_period_names) < 200:   # lets the hourly summary / logs prove what the book calls them
            _seen_period_names.add(tag)
            logger.info("sportybet period market seen: id=%s name=%r spec=%r", tag[0], tag[1], m.get("specifier"))
        per = _period_key(per, m)
        if kind == "spread":
            ln = fnum(sp.get("hcp"))
            sides = _two_sides(m.get("outcomes") or [], "spread", ln)
            if ln is not None and sides:
                cand.setdefault(per, {}).setdefault("spread", []).append((ln, sides["home"], sides["away"]))
        else:
            ln = fnum(sp.get("total"))
            sides = _two_sides(m.get("outcomes") or [], "total")
            if ln is not None and sides:
                cand.setdefault(per, {}).setdefault("total", []).append((ln, sides["over"], sides["under"]))
    out = {}
    for per, d in cand.items():
        row = {k: most_balanced(v) for k, v in d.items()}
        row = {k: v for k, v in row.items() if v}
        if row:
            out[per] = row
    return out


def parse_events(payload: Dict, league_id: Optional[str] = None, book: str = BOOK) -> List[Dict]:
    rows: List[Dict] = []
    for t in ((payload or {}).get("data") or {}).get("tournaments") or []:
        league = wanted(f'{t.get("categoryName", "")} {t.get("name", "")}', league_id)
        if not league:
            continue
        for ev in t.get("events") or []:
            home, away = ev.get("homeTeamName"), ev.get("awayTeamName")
            ml, sp_c, tt_c = None, [], []
            for m in ev.get("markets") or []:
                mid, outs = str(m.get("id")), m.get("outcomes") or []
                if mid == "219" and len(outs) == 2:
                    a, b = _odds(outs[0]), _odds(outs[1])
                    ml = (a, b) if a and b else None
                elif mid == "223" and len(outs) == 2:
                    ln = fnum(_spec(m.get("specifier")).get("hcp"))
                    a, b = _odds(outs[0]), _odds(outs[1])
                    if ln is not None and a and b:
                        sp_c.append((ln, a, b))               # hcp is from the HOME side
                elif mid == "225" and len(outs) == 2:
                    ln = fnum(_spec(m.get("specifier")).get("total"))
                    by = {str(o.get("desc", "")).lower().split(" ")[0]: _odds(o) for o in outs}
                    if ln is not None and by.get("over") and by.get("under"):
                        tt_c.append((ln, by["over"], by["under"]))
            sp, tt = most_balanced(sp_c), most_balanced(tt_c)
            rows.extend(build_rows(book, league, ev.get("eventId"), home, away,
                                   to_iso(ev.get("estimateStartTime")), ml=ml,
                                   spread=sp, total=tt, periods=_period_rows(ev)))
    return rows


_last_discovery = {"t": 0.0}


async def _discover_markets() -> None:
    """Once an hour ask the PRE-MATCH feed for ALL markets of the first page (no marketId filter) and log every
    unfamiliar market id once (_log_unknown_market), so quarter / half market ids can be read from the logs.
    Log only: nothing parsed here is ever used for a bet."""
    if os.getenv("SPORTYBET_DISCOVER", "0") != "1" or time.monotonic() - _last_discovery["t"] < 3600:
        return
    _last_discovery["t"] = time.monotonic()
    try:
        payload = await get_json(URL, headers=HEADERS, params={
            "sportId": "sr:sport:2", "marketId": "", "pageSize": 30, "pageNum": 1, "todayGames": "false", "timeline": ""})
        for t in ((payload or {}).get("data") or {}).get("tournaments") or []:
            for ev in (t.get("events") or [])[:6]:
                for m in ev.get("markets") or []:
                    _log_unknown_market(dict(period="pre", matchStatus="prematch"), m)
    except Exception as e:
        logger.info("sportybet market discovery skipped: %s", e)


async def _pages() -> List[Dict]:
    """Raw feed pages, cached for SPORTYBET_CACHE_S seconds (empty results are never cached)."""
    if _cache["pages"] and time.monotonic() - _cache["t"] < CACHE_S:
        return _cache["pages"]
    await _discover_markets()
    pages: List[Dict] = []
    for page in range(1, PAGES + 1):
        payload = await get_json(URL, headers=HEADERS, params={
            "sportId": "sr:sport:2", "marketId": MARKET_IDS, "pageSize": 100,
            "pageNum": page, "todayGames": "false", "timeline": ""})
        if not payload or not (payload.get("data") or {}).get("tournaments"):
            break
        if page == 1:
            log_shape_once(BOOK, payload)
            save_capture(BOOK, payload)
        pages.append(payload)
    if pages:
        _cache["t"], _cache["pages"] = time.monotonic(), pages
    return pages


async def fetch_odds(league_id=None, *args, **kwargs) -> List[Dict]:
    rows: List[Dict] = []
    for payload in await _pages():
        rows.extend(parse_events(payload, league_id))
    return rows


# ======================================================================================================
# LIVE  (factsCenter/liveOrPrematchEvents, found in SportyBet's own site code; GET, answers
#        {"bizCode": 10000, "data": [ {tournament: {events: [...]}} ]} - data is a LIST of tournaments).
# Event fields used: eventId, homeTeamName, awayTeamName, setScore "60:54", period "3", matchStatus "Q3",
#   playedSeconds "29:25" (game clock, whole game), remainingTimeInPeriod "00:35", sport.category.name.
# Market fields used: id, specifier, status (0 = open; anything else is skipped), banned, availableScore (the
#   score the odds are quoted for - must equal the event's current setScore), outcomes[id, desc, odds, isActive].
# ======================================================================================================
LIVE_URL = os.getenv("SPORTYBET_LIVE_URL", URL.rsplit("/", 1)[0] + "/liveOrPrematchEvents")
LIVE_CACHE_S = float(os.getenv("SPORTYBET_LIVE_CACHE_S", "8"))
LIVE_MIN_PLAYED_S = int(os.getenv("LIVE_MIN_PLAYED_S", "120"))       # skip the first 2 minutes
LIVE_END_BUFFER_S = int(os.getenv("LIVE_END_BUFFER_S", "120"))       # skip the last 2 minutes of the final period
LIVE_GARBAGE_MARGIN = int(os.getenv("LIVE_GARBAGE_MARGIN", "25"))    # skip final-period blowouts
SNAPSHOT_LOG = os.getenv("LIVE_SNAPSHOT_LOG", "1") != "0"
SNAPSHOT_EVERY_S = 30
SNAPSHOT_TO_LOG = os.getenv("LIVE_SNAPSHOT_TO_LOG", "1") != "0"


def _fmt_for(league: str):
    """(periods, seconds per period) exactly as core.live_model assumes for this league."""
    try:
        from core.live_model import fmt
        return fmt(league)
    except Exception:                      # pragma: no cover - only if live_model cannot be imported
        return (2, 1200) if league == "ncaab" else (4, 600)


_SIDE_ID = {"4": "home", "5": "away", "1714": "home", "1715": "away", "12": "over", "13": "under"}
_live_cache: Dict = {"t": 0.0, "payload": None}
_last_snap: Dict[str, float] = {}


def _side(o: Dict) -> Optional[str]:
    """Which side an outcome is - by id, else by its description. Never by position."""
    s = _SIDE_ID.get(str(o.get("id")))
    if s:
        return s
    d = str(o.get("desc", "")).strip().lower()
    return next((k for k in ("home", "away", "over", "under") if d.startswith(k)), None)


def _score(s):
    try:
        a, b = str(s).split(":")
        return int(a), int(b)
    except (ValueError, TypeError):
        return None


_seen_live_mkts: set = set()
_live_names: Dict[str, str] = {}          # every live market id seen -> its name (first seen)
_live_period_names: set = set()           # (id, name, game status) of live markets that name a quarter or a half
_PERIOD_NAME = re.compile(r"(?i)quarter|half|\bq[1-4]\b|\b[1-4](?:st|nd|rd|th)\b")


def _note_live_market(ev: Dict, m: Dict) -> None:
    mid = str(m.get("id"))
    name = str(m.get("desc") or m.get("name") or m.get("title") or "")[:80]
    if len(_live_names) < 400:
        _live_names.setdefault(mid, name)
    if _PERIOD_NAME.search(name) and len(_live_period_names) < 100:
        _live_period_names.add((mid, name, str(ev.get("matchStatus") or "")))


def live_market_report() -> Dict:
    """What SportyBet really lists for live basketball games: lets the hourly summary say whether quarter / half
    markets exist live, instead of anyone guessing."""
    return dict(types=len(_live_names), period_markets=sorted(_live_period_names)[:8])


def _log_unknown_market(ev: Dict, m: Dict) -> None:
    """Quarter / half markets are not parsed yet: log each unknown market id ONCE (name, specifier, outcome
    labels, game period) so the real ids can be read from the logs instead of guessed."""
    mid = str(m.get("id"))
    if mid in _seen_live_mkts or mid in ("219", "223", "225"):
        return
    _seen_live_mkts.add(mid)
    try:
        outs = [(o.get("id"), o.get("desc"), o.get("odds")) for o in (m.get("outcomes") or [])][:4]
        name = m.get("desc") or m.get("name") or m.get("title") or ""
        logger.info("sportybet live market seen: id=%s name=%r spec=%r period=%s status=%s outcomes=%s keys=%s",
                    mid, name, m.get("specifier"), ev.get("period"), ev.get("matchStatus"), outs, sorted(m.keys())[:14])
    except Exception:  # pragma: no cover - logging must never break a scan
        pass


def _live_markets(ev: Dict, score_str: str):
    ml, sp_c, tt_c = None, [], []
    for m in ev.get("markets") or []:
        try:
            _note_live_market(ev, m)
        except Exception:  # pragma: no cover
            pass
        _log_unknown_market(ev, m)
        if str(m.get("status")) != "0" or m.get("banned"):
            continue                                      # suspended / closed
        av = m.get("availableScore")
        if av is not None and av != score_str:
            continue                                      # odds were quoted for another score = stale
        sides = {}
        for o in m.get("outcomes") or []:
            sd, px = _side(o), _odds(o)
            if sd and px:
                sides[sd] = px
        spec, mid = _spec(m.get("specifier")), str(m.get("id"))
        if mid == "219" and "home" in sides and "away" in sides:
            ml = (sides["home"], sides["away"])
        elif mid == "223" and "home" in sides and "away" in sides:
            ln = fnum(spec.get("hcp"))
            if ln is not None:
                sp_c.append((ln, sides["home"], sides["away"]))
        elif mid == "225" and "over" in sides and "under" in sides:
            ln = fnum(spec.get("total"))
            if ln is not None:
                tt_c.append((ln, sides["over"], sides["under"]))
    return ml, most_balanced(sp_c), most_balanced(tt_c)


def _break_after(status: str, period: int, n: int) -> Optional[int]:
    """Which period just ended when the game is on a break: '1st break' -> 1, '2nd break' / half-time -> 2 (n // 2),
    '3rd break' -> 3. None when the feed does not say (the engine then records nothing for that break)."""
    m = re.search(r"(\d)\s*(?:st|nd|rd|th)\b", status or "", re.I)
    if m and 1 <= int(m.group(1)) < n:
        return int(m.group(1))
    if re.search(r"(?i)half ?-?time|\bht\b", status or ""):
        return n // 2
    return period if 1 <= period < n else None


def _parse_live_event(tour: Dict, ev: Dict, league_id: Optional[str]) -> Optional[Dict]:
    cat = ((ev.get("sport") or {}).get("category") or {}).get("name", "")
    league = wanted(f'{cat} {tour.get("name", "")}', league_id)
    home, away, eid = ev.get("homeTeamName"), ev.get("awayTeamName"), ev.get("eventId")
    sc = _score(ev.get("setScore"))
    if not (league and home and away and eid and sc):
        return None
    n, cfg_len = _fmt_for(league)
    status = str(ev.get("matchStatus") or "")
    try:
        period = int(ev.get("period"))
    except (TypeError, ValueError):
        period = 0
    if re.search(r"(?i)\bot\b|over ?time", status) or period > n:
        return None                                       # overtime: the model has no period length for it
    on_break = bool(re.search(r"(?i)half ?-?time|\bht\b|break|pause|interval", status))   # NOT "2nd half"
    played, rem = parse_clock(ev.get("playedSeconds")), parse_clock(ev.get("remainingTimeInPeriod"))
    brk = _break_after(status, period, n) if on_break else None
    if on_break:
        period, elapsed = 0, 0.0
    else:
        if period < 1 or played is None or rem is None:
            return None
        length = (played + rem) / period                  # period length, derived from the feed itself
        # CLAUDE FIX: live_model.frac_remaining() assumes cfg_len seconds per period. If the feed's own period
        # length disagrees (a different format), the remaining-time fraction would be silently wrong -> skip.
        if abs(length - cfg_len) > 20 or rem > cfg_len + 1:
            return None
        elapsed = cfg_len - rem                           # GAME seconds into the current period
    ml, sp, tt = _live_markets(ev, str(ev.get("setScore")))
    rows = build_rows(BOOK, league, eid, home, away, to_iso(ev.get("estimateStartTime")), ml=ml, spread=sp, total=tt)
    return dict(id=str(eid), league=league, home=home, away=away, home_score=sc[0], away_score=sc[1],
                period=period, halftime=on_break, break_after=brk, elapsed=float(elapsed), played=played or 0,
                remaining=rem if rem is not None else 0, source=BOOK, rows=rows)


def parse_live(payload: Dict, league_id: Optional[str] = None) -> List[Dict]:
    data = (payload or {}).get("data")
    if not isinstance(data, list):
        return []
    out = []
    for tour in data:
        for ev in (tour or {}).get("events") or []:
            try:
                g = _parse_live_event(tour or {}, ev, league_id)
            except Exception as e:
                logger.debug("sportybet live event skipped: %s", e)
                continue
            if g:
                out.append(g)
    return out


def tradable(g: Dict) -> bool:
    """No Live alert without a real clock, and not in the first/last minutes or in a blowout."""
    if g["halftime"] or not g["period"] or g["played"] < LIVE_MIN_PLAYED_S:
        return False
    if g["period"] == _fmt_for(g["league"])[0]:
        if g["remaining"] < LIVE_END_BUFFER_S or abs(g["home_score"] - g["away_score"]) >= LIVE_GARBAGE_MARGIN:
            return False
    return True


def _snapshot(games: List[Dict]) -> None:
    """Append-only log of live prices + score + clock (data/live_log/YYYY-MM-DD.jsonl, one line per game per
    30 s) so the live model can be checked against real results before any live alert is trusted."""
    if not SNAPSHOT_LOG:
        return
    try:
        now = time.time()
        d = os.path.join(os.getenv("BEAST_DATA_DIR", "data"), "live_log")
        for g in games:
            if now - _last_snap.get(g["id"], 0.0) < SNAPSHOT_EVERY_S or not g["rows"]:
                continue
            _last_snap[g["id"]] = now
            os.makedirs(d, exist_ok=True)
            rec = dict(ts=int(now), league=g["league"], home=g["home"], away=g["away"], hs=g["home_score"],
                       as_=g["away_score"], period=g["period"], elapsed=g["elapsed"], remaining=g["remaining"],
                       rows=[[r["market"], r["side"], r["line"], r["odds"]] for r in g["rows"]])
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            line = json.dumps(rec, separators=(",", ":"))
            with open(os.path.join(d, f"{day}.jsonl"), "a") as f:
                f.write(line + "\n")
            if SNAPSHOT_TO_LOG:   # Render's disk cannot be read from outside; the log can (replay of the live model)
                logger.info("LIVESNAP %s", line)
    except Exception as e:
        logger.debug("live snapshot not written: %s", e)


async def _live_payload() -> Optional[Dict]:
    """Live feed, cached a few seconds so one request serves every game and league in a scan."""
    if _live_cache["payload"] is not None and time.monotonic() - _live_cache["t"] < LIVE_CACHE_S:
        return _live_cache["payload"]
    p = await get_json(LIVE_URL, headers=HEADERS, retries=1, params={
        "sportId": "sr:sport:2", "marketId": MARKET_IDS, "pageSize": 100, "pageNum": 1})
    if not isinstance(p, dict) or str(p.get("bizCode")) != "10000" or not isinstance(p.get("data"), list):
        _live_cache["payload"] = None                    # never reuse a stale live payload
        return None
    log_shape_once(BOOK + "_live", p)
    save_capture(BOOK + "_live", p)
    _live_cache.update(t=time.monotonic(), payload=p)
    return p


async def _games(league_id: Optional[str] = None) -> List[Dict]:
    games = parse_live(await _live_payload(), league_id)
    _snapshot(games)
    try:
        from core import finals
        finals.shared().observe(games)       # last score of every live game, for grading (core/finals.py)
    except Exception as e:
        logger.debug("finals not updated: %s", e)
    return games


async def fetch_native_live(league, home, away, *args, **kwargs) -> List[Dict]:
    """Interface used by services.scraper.scrape_live_soft: rows for THIS game only, none unless the game
    passes the live guards (clock present, not suspended, not in the first/last minutes, not a blowout)."""
    for g in await _games(league):
        if same_team(g["home"], home) and same_team(g["away"], away):
            if not tradable(g):
                return []
            meta = dict(live=True, home_score=g["home_score"], away_score=g["away_score"],
                        period=g["period"], elapsed=g["elapsed"], remaining=g["remaining"])
            return [{**r, **meta} for r in g["rows"]]
    return []


def live_games_sync(league_id: Optional[str] = None) -> List[Dict]:
    """Live games in the shape core.scores.sofascore.live_games() used to return (id, league, home, away,
    home_score, away_score, period, halftime, elapsed = GAME seconds into the period) - only tradable games."""
    from core.bookmakers.soft_feed import _run
    # CLAUDE FIX: games on a break (quarter break / half-time) must reach the scanner too. The quarter outlook records the
    # score at each break (Q1 Break, half-time, Q3 Break) to build its rows; tradable() dropped every break, so those
    # rows never appeared. The live scanner itself still skips halftime games after recording the outlook.
    return [{k: v for k, v in g.items() if k != "rows"} for g in _run(_games(league_id))
            if tradable(g) or (g.get("halftime") and g.get("break_after"))]


def fixtures_sync(days: int = 2) -> List[Dict]:
    """Games still to start in the next `days` days, from the prematch feed, in the shape of
    core.scores.sofascore.fixtures() (used when Sofascore answers 403)."""
    from core.bookmakers.soft_feed import _run
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    end, seen, out = now + timedelta(days=days), set(), []
    for payload in _run(_pages()):
        for t in ((payload or {}).get("data") or {}).get("tournaments") or []:
            league = wanted(f'{t.get("categoryName", "")} {t.get("name", "")}', None)
            if not league:
                continue
            for ev in t.get("events") or []:
                ts, eid = fnum(ev.get("estimateStartTime")), ev.get("eventId")
                if not (ev.get("homeTeamName") and ev.get("awayTeamName") and ts and eid) or eid in seen:
                    continue
                commence = datetime.fromtimestamp(ts / 1000.0 if ts > 1e11 else ts, timezone.utc).replace(tzinfo=None)
                if now <= commence <= end:
                    seen.add(eid)
                    out.append(dict(id=str(eid), league=league, home=ev["homeTeamName"], away=ev["awayTeamName"],
                                    commence=commence))
    return out
