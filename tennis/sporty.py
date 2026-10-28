"""SportyBet tennis feed (the same factsCenter endpoint the basketball and football engines read, sportId sr:sport:5).
Markets are recognised by NAME + specifier, not by id alone (tennis market ids are not documented anywhere we can check from
outside): a two-way 'winner' market without a line is the match winner, 'total games' with total=<line> is total games, a
'handicap games' market with hcp=<line> is the game handicap. Sides are read from the outcome text (player names / over /
under) or the outcome id, never from the order. Set / tiebreak / first-set markets are ignored. All full match."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import Counter

from tennis import model
from tennis.names import bad_league, same_side
from services.client import get_json, HttpError

log = logging.getLogger("tennis.sporty")
URL = os.getenv("SPORTYBET_URL", "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents")
HEADERS = {"operid": "2", "clientid": "web", "platform": "web", "Referer": "https://www.sportybet.com/ng/"}
MARKET_IDS = os.getenv("TN_SPORTY_MARKET_IDS", "")            # empty = every market of each game (names decide)
PAGE_SIZE = int(os.getenv("TN_SPORTY_PAGE_SIZE", "50"))
SKIP_WORDS = ("set", "tiebreak", "tie-break", "tie break", "1st", "2nd", "3rd", "4th", "5th", "first", "second", "third", "ace",
              "fault", "break", "point", "player", "odd", "even", "exact", "correct", "race", "to win a", "any", "specials", "live",
              "most", "both", "winning margin", "each")
SAMPLES = {}                          # market name -> one real example (specifier + outcome names), for markets we do not compare yet
SEEN = Counter()                      # market names seen on SportyBet tennis games (diagnostics, shown in /status)


def _spec(s) -> dict:
    out = {}
    for part in re.split(r"[&|]", str(s or "")):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _num(x):
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _price(o: dict):
    if o.get("isActive") in (0, False):
        return None
    v = _num(o.get("odds"))
    return v if v and v > 1.0 else None


def _name(m: dict) -> str:
    return str(m.get("desc") or m.get("name") or m.get("title") or "").strip().lower()


def _kind(m: dict, spec: dict, n_out: int):
    """-> 'ml' | 'total' | 'ah' | None from the market name, specifier and outcome count."""
    nm = _name(m)
    if not nm or n_out != 2:
        return None
    if nm in ("set handicap", "sets handicap", "handicap sets") and _num(spec.get("hcp")) is not None and abs(_num(spec.get("hcp"))) in (1.5, 2.5):
        return "sh"                   # whole-match set handicap (who wins by sets): -1.5 means winning in straight sets
    if any(w in nm for w in SKIP_WORDS if w != "set") or re.search(r"\bsets?\b", nm):
        # 'match winner' / 'total games' never mention sets; 'set handicap' / '1st set ...' are skipped
        return None
    words = re.findall(r"[a-z/]+", nm)
    generic = {"total", "totals", "games", "game", "over", "under", "over/under", "o/u", "number", "of", "match", "full", "handicap",
               "asian", "spread", "the", "in", "time", "winner", "to", "win", "moneyline", "result", "way"}
    tot = "total" in nm or "over/under" in nm or "over under" in nm or "o/u" in nm
    if (tot or "handicap" in nm or "spread" in nm) and any(w not in generic for w in words):
        return None            # a player's or team's own total / handicap ('Cobolli total games'): never the match market
    if tot and ("game" in nm or (_num(spec.get("total")) or 0) >= 12):
        return "total" if _num(spec.get("total")) is not None else None
    if ("handicap" in nm or "spread" in nm) and ("game" in nm or abs(_num(spec.get("hcp")) or 0) >= 3):
        return "ah" if _num(spec.get("hcp")) is not None else None
    if nm in ("winner", "match winner", "to win match", "to win the match", "moneyline", "match result", "1x2", "2way", "2-way") \
            and not spec:
        return "ml"
    if "winner" in nm and not spec and "game" not in nm:
        return "ml"
    return None


def _player_side(o: dict, home: str, away: str):
    d = str(o.get("desc") or "").strip()
    d = re.sub(r"\s*\(?[+-]\d+(?:\.\d+)?\)?\s*$", "", d)          # 'Djokovic (-3.5)' -> 'Djokovic'
    if not d:
        return None
    h, a = same_side(d, home), same_side(d, away)
    if h and h > a:
        return "home"
    if a and a > h:
        return "away"
    dl = d.lower()
    if dl in ("1", "home"):
        return "home"
    if dl in ("2", "away"):
        return "away"
    return None


_ID_SIDE = {"4": "home", "5": "away", "1714": "home", "1715": "away"}


def extras(markets: list) -> list:
    """Plain names of the SportyBet markets on a game that are NOT compared with Pinnacle (shown on the card, never bet)."""
    rules = [("Set handicap", lambda n, w: n.startswith("set handicap")),
             ("Exact sets", lambda n, w: n == "exact sets"),
             ("Correct score (sets)", lambda n, w: n == "correct score"),
             ("Total sets", lambda n, w: "set" in w and ("total" in n or "over" in n) and "game" not in n),
             ("First set winner", lambda n, w: ("first set" in n or "1st set" in n) and "winner" in n),
             ("Set winner", lambda n, w: "set" in w and "winner" in n and "first set" not in n and "1st set" not in n),
             ("Tiebreak", lambda n, w: "tiebreak" in n or "tie-break" in n or "tie break" in n),
             ("Player total games", lambda n, w: ("total" in n or "over" in n) and "game" in n and "match" not in n and n not in ("total games", "games over/under")),
             ("Odd / even games", lambda n, w: "odd" in n and "even" in n)]
    out = []
    for m in markets or []:
        nm = _name(m)
        if not nm or m.get("status") not in (None, 0, "0") or m.get("banned"):
            continue
        w = ["set" if x == "sets" else x for x in re.findall(r"[a-z]+", nm)]
        for label, f in rules:
            if label not in out and f(nm, w):
                out.append(label)
                break
    return out


def _flat_sets(m: dict, outs: list):
    """Whole-match set markets of a best-of-3, recognised by their exact outcome names (never by the market name alone):
    correct score in sets {2:0, 2:1, 1:2, 0:2} and exact sets {2, 3}. -> ('cs' | 'es', {selection: odds}) or None."""
    nm = _name(m)
    names = {str(o.get("desc") or "").strip(): _price(o) for o in outs}
    if nm == "correct score" and set(names) == {"2:0", "2:1", "1:2", "0:2"} and all(names.values()):
        return "cs", dict(names)
    if nm == "exact sets" and set(names) == {"2", "3"} and all(names.values()):
        return "es", {"s" + k: v for k, v in names.items()}
    return None


def parse_markets(markets: list, home: str, away: str) -> dict:
    ev = {"markets": {}}
    for m in markets or []:
        if m.get("status") not in (None, 0, "0") or m.get("banned"):
            continue
        outs, sp = m.get("outcomes") or [], _spec(m.get("specifier"))
        SEEN[_name(m)[:40] + (" |" + ",".join(sorted(sp)) if sp else "")] += 1
        nm0 = _name(m)
        if nm0 and nm0 not in SAMPLES and len(SAMPLES) < 40 and not nm0.startswith(("total games", "game handicap", "set handicap")) and nm0 != "winner" \
                and " total games" not in nm0:
            SAMPLES[nm0] = "%s | %s | %s" % (nm0[:30], str(m.get("specifier") or "")[:24],
                                             "; ".join("%s=%s" % (str(o.get("desc"))[:14], o.get("odds")) for o in outs[:8]))
        flat = _flat_sets(m, outs)
        if flat:
            kind, got = flat
            for sel, px in got.items():
                model.ladder_put(ev, kind, None, sel, px)
            continue
        kind = _kind(m, sp, len(outs))
        if not kind:
            continue
        line = None
        if kind == "total":
            line = _num(sp.get("total"))
        elif kind in ("ah", "sh"):
            line = _num(sp.get("hcp"))
        got = {}
        for o in outs:
            px = _price(o)
            if not px:
                continue
            if kind == "total":
                d = str(o.get("desc") or "").strip().lower()
                s = "over" if d.startswith("over") else ("under" if d.startswith("under") else {"12": "over", "13": "under"}.get(str(o.get("id"))))
            else:
                s = _player_side(o, home, away) or _ID_SIDE.get(str(o.get("id")))
            if s and s not in got:
                got[s] = px
        want = ("over", "under") if kind == "total" else ("home", "away")
        if all(w in got for w in want):
            for s, px in got.items():
                model.ladder_put(ev, kind, line, s, px)
    return ev["markets"]


def parse_events(payload: dict) -> list:
    """-> [{id, lid, lname, home, away, t, markets}] for every real tennis match with a start time."""
    out = []
    data = (payload or {}).get("data")
    tours = data.get("tournaments") if isinstance(data, dict) else data
    for t in tours or []:
        if not isinstance(t, dict):
            continue
        cat, name = str(t.get("categoryName") or ""), str(t.get("name") or "")
        if bad_league("%s %s" % (cat, name)):
            continue
        lid = str(t.get("id") or t.get("tournamentId") or "")
        for ev in t.get("events") or []:
            home, away, ms = ev.get("homeTeamName"), ev.get("awayTeamName"), ev.get("estimateStartTime")
            if not (home and away and ms):
                continue
            try:
                ts = float(ms) / 1000.0 if float(ms) > 1e11 else float(ms)
            except (TypeError, ValueError):
                continue
            mk = parse_markets(ev.get("markets"), str(home), str(away))
            out.append(dict(id=str(ev.get("eventId")), lid=lid or "%s|%s" % (cat, name), lname="%s - %s" % (cat, name) if cat else name,
                            country=cat, tname=name, home=str(home), away=str(away), t=ts, markets=mk, extra=extras(ev.get("markets"))))
    return out


class SportyFeed:
    """Cached tennis sweeps. 'upcoming' = every coming match (paged, refreshed every UPCOMING_S); 'today' = today's matches
    only (a short sweep, refreshed on demand for the last-minute checks)."""
    UPCOMING_S = float(os.getenv("TN_SPORTY_CACHE_S", "420"))
    TODAY_S = float(os.getenv("TN_SPORTY_TODAY_S", "60"))
    MAX_PAGES = int(os.getenv("TN_SPORTY_PAGES", "60"))

    def __init__(self, get=None, clock=None):
        self._get = get or get_json
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self.events = {}
        self.sweeps = {"upcoming": 0.0, "today": 0.0}
        self.last = dict(pages=0, events=0, error=None)

    # request shapes tried in order until SportyBet accepts one (it answers 422 'Invalid' to a shape it does not like)
    VARIANTS = [dict(marketId=MARKET_IDS, pageSize=PAGE_SIZE), dict(marketId="", pageSize=30), dict(pageSize=30),
                dict(marketId=",".join(str(i) for i in range(186, 201)), pageSize=50),      # the tennis market family (winner, handicaps, totals ...)
                dict(marketId="186,187,188", pageSize=50), dict(marketId="", pageSize=100)]

    def _params(self, v: dict, page: int, today: bool) -> dict:
        p = {"sportId": "sr:sport:5", "pageNum": page, "todayGames": "true" if today else "false", "timeline": ""}
        p.update(v)
        return p

    def _sweep(self, today: bool) -> int:
        n_ev, pages = 0, 0
        order = [getattr(self, "variant", None)] if getattr(self, "variant", None) is not None else list(range(len(self.VARIANTS)))
        vi = None
        for page in range(1, self.MAX_PAGES + 1):
            payload = None
            for i in (order if vi is None else [vi]):
                try:
                    payload, _ = self._get(URL, self._params(self.VARIANTS[i], page, today), HEADERS, 20, 2)
                    vi = i
                    self.variant = i
                    break
                except HttpError as e:
                    self.last["error"] = str(e)[:120]
                    log.warning("sportybet tennis page %d (shape %d) failed: %s", page, i, str(e)[:100])
                    if vi is not None or e.status not in (400, 422):
                        break
            if payload is None:
                if vi is None and page == 1:
                    self.variant = None
                break
            data = (payload or {}).get("data")
            tours = data.get("tournaments") if isinstance(data, dict) else data
            if not tours:
                break
            pages += 1
            evs = parse_events(payload)
            for e in evs:
                self.events[e["id"]] = e
            n_ev += len(evs)
            time.sleep(0.15)
        if pages:
            self.last.update(pages=pages, events=n_ev, error=None, shape=vi)
        return n_ev

    def refresh(self, today_only: bool = False, force: bool = False) -> int:
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
        for k in [k for k, e in self.events.items() if e["t"] < now - 8 * 3600]:
            del self.events[k]

    def upcoming(self, horizon_s: float, now: float = None) -> list:
        now = now or self._clock()
        return sorted((e for e in self.events.values() if now - 600 <= e["t"] <= now + horizon_s), key=lambda e: e["t"])
