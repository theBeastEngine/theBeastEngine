"""SportyBet ice hockey feed (the same factsCenter endpoint the basketball engine reads, sportId sr:sport:4).
Only FULL-GAME INCLUDING OVERTIME markets are used, because that is what Pinnacle prices: 219 = winner (moneyline),
223 = handicap (puck line), 225 = total goals. The regulation-only markets (1X2 three-way, ids 1 / 16 / 18) are never compared.
A market is also checked by its name: anything that says 3-way, regulation, period, team total etc. is dropped, and every market
name seen is kept in SEEN so the real names can be read in /status."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import Counter

from hockey import model
from hockey.names import bad_league
from services.client import get_json, HttpError

log = logging.getLogger("hockey.sporty")
URL = os.getenv("SPORTYBET_URL", "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents")
HEADERS = {"operid": "2", "clientid": "web", "platform": "web", "Referer": "https://www.sportybet.com/ng/"}
MARKET_IDS = os.getenv("HK_SPORTY_MARKET_IDS", "1,16,18,219,223,225,406,410,412,413,414,415,416,417,418,419,420,421,422,423,424,425,426,427,428,429,430,431,432,433,434,435,436,437,438,439,440,441,442,443,444,445,446,447,448,449,450")   # wide list: ids that do not exist for hockey simply return nothing; names decide what is used.
# the book rejects an empty list with 100 per page (HTTP 422): the all-markets look is a separate small probe
PAGE_SIZE = int(os.getenv("HK_SPORTY_PAGE_SIZE", "100"))
SEEN = Counter()                      # (market id, market name) -> times seen: proof of what the book calls them
_BAD_NAME = ("3-way", "3 way", "three way", "regulation", "regular", "period", "1st", "2nd", "3rd", "team total", "exact", "odd",
             "even", "both", "race", "first", "last", "draw no bet", "double chance", "overtime only")


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


_ML = {"4": "home", "5": "away"}
_AH = {"1714": "home", "1715": "away"}
_OU = {"12": "over", "13": "under"}


def _kind(m: dict, mid: str, sp: dict, n_out: int):
    """'ml' | 'total' | 'ah' | None. Ids 219 / 225 / 223 (the incl.-overtime markets) are taken as they are; any other market is
    taken only when its NAME says it is the full game including overtime (the ids of the book's hockey markets are not documented)."""
    if mid == "219":
        return "ml"
    if mid == "225":
        return "total"
    if mid == "223":
        return "ah"
    nm = str(m.get("desc") or m.get("name") or m.get("title") or "").lower()
    if not any(w in nm for w in ("overtime", "incl. ot", "incl ot", "including ot", "incl. extra", "incl extra")):
        return None
    if "handicap" in nm and "hcp" in sp:
        return "ah"
    if ("total" in nm or "over/under" in nm) and "total" in sp:
        return "total"
    if n_out == 2 and not sp and any(w in nm for w in ("winner", "moneyline", "money line", "to win", "match")):
        return "ml"
    return None


def seen_top(n: int = 8) -> list:
    return [(k[0], k[1], c) for k, c in SEEN.most_common(n)]


def _ok_name(m: dict) -> bool:
    nm = str(m.get("desc") or m.get("name") or m.get("title") or "").lower()
    return not any(w in nm for w in _BAD_NAME)


def parse_markets(markets: list) -> dict:
    ev = {"markets": {}}
    for m in markets or []:
        if m.get("status") not in (None, 0, "0") or m.get("banned"):
            continue
        mid, outs, sp = str(m.get("id")), m.get("outcomes") or [], _spec(m.get("specifier"))
        if len(SEEN) < 300:
            SEEN[(mid, str(m.get("desc") or m.get("name") or "")[:50])] += 1
        kind = _kind(m, mid, sp, len(outs))
        if kind is None or not _ok_name(m):
            continue
        if kind == "ml":
            if len(outs) != 2:
                continue
            for o in outs:
                s, px = _side(o, _ML, ("home", "away")), _price(o)
                if s and px:
                    model.ladder_put(ev, "ml", None, s, px)
        elif kind == "total":
            ln = _num(sp.get("total"))
            if ln is None:
                continue
            for o in outs:
                s, px = _side(o, _OU, ("over", "under")), _price(o)
                if s and px:
                    model.ladder_put(ev, "total", ln, s, px)
        else:
            ln = _num(sp.get("hcp"))
            if ln is None:
                continue
            for o in outs:
                s, px = _side(o, _AH, ("home", "away")), _price(o)
                if s and px:
                    model.ladder_put(ev, "ah", ln, s, px)
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
    """Cached ice hockey sweeps. 'upcoming' = every coming game (paged, refreshed every UPCOMING_S); 'today' = today's games only
    (a short sweep, refreshed on demand for the last-minute checks)."""
    UPCOMING_S = float(os.getenv("HK_SPORTY_CACHE_S", "420"))
    TODAY_S = float(os.getenv("HK_SPORTY_TODAY_S", "60"))
    MAX_PAGES = int(os.getenv("HK_SPORTY_PAGES", "40"))

    def __init__(self, get=None, clock=None):
        self._get = get or get_json
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self.events = {}            # id -> event (latest seen)
        self.sweeps = {"upcoming": 0.0, "today": 0.0}
        self.last = dict(pages=0, events=0, error=None)
        self._probe_t = 0.0

    def probe(self, now: float):
        """Once an hour: ONE small request for ALL markets of the first games, only to record every market id and name the book
        really uses for hockey (SEEN, shown in /status). Never feeds the engine and never raises."""
        if now - self._probe_t < 3600:
            return
        self._probe_t = now
        tries = [{"marketId": "", "pageSize": 30}, {"pageSize": 10}, {"marketId": "1,16,18", "pageSize": 20}]
        errs = []
        for extra in tries:
            try:
                params = {"sportId": "sr:sport:4", "pageNum": 1, "todayGames": "false", "timeline": ""}
                params.update(extra)
                payload, _ = self._get(URL, params, HEADERS, 20, 1)
                data = (payload or {}).get("data")
                n = 0
                for t in (data.get("tournaments") if isinstance(data, dict) else data) or []:
                    for ev in (t.get("events") or [] if isinstance(t, dict) else []):
                        parse_markets(ev.get("markets"))
                        n += 1
                self.last["probe"] = "ok (%d games, request %s)" % (n, sorted(extra))
                return
            except Exception as e:
                errs.append("%s: %s" % (sorted(extra), str(e)[:60]))
                log.warning("sportybet hockey market probe failed: %s", errs[-1])
        self.last["probe"] = "failed: " + " | ".join(errs)

    def _sweep(self, today: bool) -> int:
        n_ev, pages = 0, 0
        for page in range(1, self.MAX_PAGES + 1):
            params = {"sportId": "sr:sport:4", "marketId": MARKET_IDS, "pageSize": PAGE_SIZE, "pageNum": page,
                      "todayGames": "true" if today else "false", "timeline": ""}
            try:
                payload, _ = self._get(URL, params, HEADERS, 20, 2)
            except HttpError as e:
                self.last["error"] = str(e)[:120]
                log.warning("sportybet hockey page %d failed: %s", page, e)
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
            self.probe(now)
            self._drop_old(now)
            return n

    def _drop_old(self, now):
        for k in [k for k, e in self.events.items() if e["t"] < now - 6 * 3600]:
            del self.events[k]

    def upcoming(self, horizon_s: float, now: float = None) -> list:
        now = now or self._clock()
        return sorted((e for e in self.events.values() if now - 600 <= e["t"] <= now + horizon_s), key=lambda e: e["t"])
