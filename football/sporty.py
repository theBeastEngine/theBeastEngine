"""SportyBet football feed (the same factsCenter endpoint the basketball engine reads, sportId sr:sport:1).
Market ids (Sportradar): 1 = 1X2, 18 = total goals, 16 = Asian handicap, 11 = draw no bet. All full time, 90 minutes."""
from __future__ import annotations

import logging
import os
import re
import threading
import time

from football import model
from football.names import bad_league
from services.client import get_json, HttpError

log = logging.getLogger("football.sporty")
URL = os.getenv("SPORTYBET_URL", "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents")
HEADERS = {"operid": "2", "clientid": "web", "platform": "web", "Referer": "https://www.sportybet.com/ng/"}
MARKET_IDS = os.getenv("FB_SPORTY_MARKET_IDS", "1,18,16,11")
PAGE_SIZE = int(os.getenv("FB_SPORTY_PAGE_SIZE", "100"))


def _spec(s) -> dict:
    out = {}
    for part in re.split(r"[&|]", str(s or "")):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _num(x):
    try:
        v = float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return v


def _price(o: dict):
    if o.get("isActive") in (0, False):
        return None
    v = _num(o.get("odds"))
    return v if v and v > 1.0 else None


def _side(o: dict, ids: dict, words: tuple):
    s = ids.get(str(o.get("id")))
    if s:
        return s
    d = str(o.get("desc") or "").strip().lower()
    return next((w for w in words if d.startswith(w)), None)


_X12 = {"1": "home", "2": "draw", "3": "away"}
_DNB = {"4": "home", "5": "away"}
_AH = {"1714": "home", "1715": "away"}
_OU = {"12": "over", "13": "under"}


def parse_markets(markets: list) -> dict:
    ev = {"markets": {}}
    dnb = {}
    for m in markets or []:
        if m.get("status") not in (None, 0, "0") or m.get("banned"):
            continue
        mid, outs, sp = str(m.get("id")), m.get("outcomes") or [], _spec(m.get("specifier"))
        if mid == "1":
            for o in outs:
                s, px = _side(o, _X12, ("home", "draw", "away")), _price(o)
                if s and px:
                    model.ladder_put(ev, "1x2", None, s, px)
        elif mid == "18":
            ln = _num(sp.get("total"))
            if ln is None:
                continue
            for o in outs:
                s, px = _side(o, _OU, ("over", "under")), _price(o)
                if s and px:
                    model.ladder_put(ev, "total", ln, s, px)
        elif mid == "16":
            ln = _num(sp.get("hcp"))
            if ln is None:
                continue
            for o in outs:
                s, px = _side(o, _AH, ("home", "away")), _price(o)
                if s and px:
                    model.ladder_put(ev, "ah", ln, s, px)
        elif mid == "11":                                   # draw no bet = asian handicap 0 (used only if no AH 0 is listed)
            for o in outs:
                s, px = _side(o, _DNB, ("home", "away")), _price(o)
                if s and px:
                    dnb[s] = px
    if len(dnb) == 2 and 0.0 not in (ev["markets"].get("ah") or {}):
        for s, px in dnb.items():
            model.ladder_put(ev, "ah", 0.0, s, px)
    return ev["markets"]


def parse_events(payload: dict) -> list:
    """-> [{id, lid, lname, home, away, t, markets}] for every real men's senior game with a start time."""
    out = []
    data = (payload or {}).get("data")
    tours = data.get("tournaments") if isinstance(data, dict) else data
    for t in tours or []:
        if not isinstance(t, dict):
            continue
        cat, name = str(t.get("categoryName") or ""), str(t.get("name") or "")
        text = "%s %s" % (cat, name)
        if bad_league(text):
            continue
        lid = str(t.get("id") or t.get("tournamentId") or "")
        for ev in t.get("events") or []:
            home, away = ev.get("homeTeamName"), ev.get("awayTeamName")
            ms = ev.get("estimateStartTime")
            if not (home and away and ms):
                continue
            if bad_league("%s %s" % (home, away)):
                continue
            try:
                ts = float(ms) / 1000.0 if float(ms) > 1e11 else float(ms)
            except (TypeError, ValueError):
                continue
            mk = parse_markets(ev.get("markets"))
            out.append(dict(id=str(ev.get("eventId")), lid=lid or "%s|%s" % (cat, name), lname="%s - %s" % (cat, name) if cat else name,
                            country=cat, tname=name, home=str(home), away=str(away), t=ts, markets=mk))
    return out


class SportyFeed:
    """Cached football sweeps. 'upcoming' = every coming game (paged, refreshed every UPCOMING_S); 'today' = today's games only
    (a short sweep, refreshed on demand for the last-minute checks)."""
    UPCOMING_S = float(os.getenv("FB_SPORTY_CACHE_S", "420"))
    TODAY_S = float(os.getenv("FB_SPORTY_TODAY_S", "60"))
    MAX_PAGES = int(os.getenv("FB_SPORTY_PAGES", "40"))

    def __init__(self, get=None, clock=None):
        self._get = get or get_json
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self.events = {}            # id -> event (latest seen)
        self.sweeps = {"upcoming": 0.0, "today": 0.0}
        self.last = dict(pages=0, events=0, error=None)

    def _sweep(self, today: bool) -> int:
        n_ev, pages = 0, 0
        for page in range(1, self.MAX_PAGES + 1):
            params = {"sportId": "sr:sport:1", "marketId": MARKET_IDS, "pageSize": PAGE_SIZE, "pageNum": page,
                      "todayGames": "true" if today else "false", "timeline": ""}
            try:
                payload, _ = self._get(URL, params, HEADERS, 20, 2)
            except HttpError as e:
                self.last["error"] = str(e)[:120]
                log.warning("sportybet football page %d failed: %s", page, e)
                break
            evs = parse_events(payload)
            tours = ((payload or {}).get("data") or {}).get("tournaments") if isinstance((payload or {}).get("data"), dict) else (payload or {}).get("data")
            if not tours:
                break
            pages += 1
            for e in evs:
                self.events[e["id"]] = e
            n_ev += len(evs)
            time.sleep(0.15)
        if pages:
            self.last.update(pages=pages, events=n_ev, error=None)
        return n_ev

    def refresh(self, today_only: bool = False, force: bool = False) -> int:
        """Returns the number of events seen in this sweep (0 = nothing fetched / cached)."""
        now = self._clock()
        with self._lock:
            if today_only:
                if not force and now - self.sweeps["today"] < self.TODAY_S:
                    return 0
                self.sweeps["today"] = now
                return self._sweep(True)
            if not force and now - self.sweeps["upcoming"] < self.UPCOMING_S:
                return 0
            self.sweeps["upcoming"] = now
            self.sweeps["today"] = now
            n = self._sweep(False)
            self._drop_old(now)
            return n

    def _drop_old(self, now):
        for k in [k for k, e in self.events.items() if e["t"] < now - 6 * 3600]:
            del self.events[k]

    def upcoming(self, horizon_s: float, now: float = None) -> list:
        now = now or self._clock()
        return sorted((e for e in self.events.values() if now - 600 <= e["t"] <= now + horizon_s), key=lambda e: e["t"])
