"""Final scores for grading, from Sofascore's free scheduled-events list (90-minute score: 'normaltime' when the game went to
extra time). A game is graded only when the feed says it is finished; postponed / cancelled games void their picks."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from hockey.names import pair_score
from services.client import get_json, HttpError

log = logging.getLogger("hockey.results")
URL = "https://api.sofascore.com/api/v1/sport/hockey/scheduled-events/%s"


def parse(payload: dict) -> list:
    out = []
    for ev in (payload or {}).get("events") or []:
        try:
            st = (ev.get("status") or {})
            typ = str(st.get("type") or "").lower()
            hs, as_ = ev.get("homeScore") or {}, ev.get("awayScore") or {}
            hg = hs.get("normaltime", hs.get("current"))
            ag = as_.get("normaltime", as_.get("current"))
            state = "finished" if typ == "finished" else ("void" if typ in ("canceled", "cancelled", "postponed", "abandoned") else "other")
            if state == "finished" and (hg is None or ag is None):
                continue
            out.append(dict(home=(ev.get("homeTeam") or {}).get("name", ""), away=(ev.get("awayTeam") or {}).get("name", ""),
                            t=float(ev.get("startTimestamp") or 0), hg=hg, ag=ag, state=state))
        except Exception:
            continue
    return out


class Results:
    TTL = 600.0

    def __init__(self, get=None, clock=None, oa=None):
        self.oa = oa               # services.odds_api.OddsAPI: its /scores is the grading source (Sofascore blocks cloud servers)
        self._oa_cache = {}        # sport key -> (fetched, events)
        self._get = get or get_json
        self._clock = clock or time.time
        self.cache = {}        # 'YYYY-MM-DD' -> (fetched, events)
        self.last_error = None

    def day(self, ds: str) -> list:
        hit = self.cache.get(ds)
        if hit and self._clock() - hit[0] < self.TTL:
            return hit[1]
        if self._clock() < getattr(self, "blocked_until", 0):
            return hit[1] if hit else []                    # the source refused us (403): do not ask again for a while
        try:
            payload, _ = self._get(URL % ds, None, {"Accept": "application/json"}, 20, 2)
            evs = parse(payload)
            self.cache[ds] = (self._clock(), evs)
            self.last_error = None
            return evs
        except HttpError as e:
            self.last_error = str(e)[:100]
            if e.status in (401, 403, 429):
                self.blocked_until = self._clock() + 6 * 3600
            log.warning("results fetch %s failed: %s", ds, e)
            return hit[1] if hit else []

    def find(self, home: str, away: str, t: float):
        d = datetime.fromtimestamp(t, tz=timezone.utc)
        days = {d.strftime("%Y-%m-%d"), datetime.fromtimestamp(t + 6 * 3600, tz=timezone.utc).strftime("%Y-%m-%d"),
                datetime.fromtimestamp(t - 6 * 3600, tz=timezone.utc).strftime("%Y-%m-%d")}
        best = None
        for ds in sorted(days):
            for r in self.day(ds):
                if r["t"] and abs(r["t"] - t) > 3 * 3600:
                    continue
                sc = pair_score(home, away, r["home"], r["away"])
                if sc >= 0.72 and (best is None or sc > best[0]):
                    best = (sc, r)
        return best[1] if best else None

    def _oa_events(self, key: str) -> list:
        hit = self._oa_cache.get(key)
        if hit and self._clock() - hit[0] < 900:
            return hit[1]
        try:
            data = self.oa.scores(key, 3)
        except Exception as e:
            log.warning("Odds API scores %s failed: %s", key, str(e)[:100])
            return hit[1] if hit else []
        data = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
        self._oa_cache[key] = (self._clock(), data)
        return data

    def find_oa(self, p: dict):
        """Final score of a pick whose Pinnacle game came from The Odds API: matched by the game's own id. None = not finished."""
        key, eid = p.get("oa_key"), p.get("oa_id")
        if not (self.oa and key and eid):
            return None
        for e in self._oa_events(key):
            if str(e.get("id")) != str(eid):
                continue
            if not e.get("completed"):
                return None
            sc = {str(x.get("name")): x.get("score") for x in (e.get("scores") or []) if isinstance(x, dict)}
            try:
                return dict(state="finished", hg=int(float(sc[e["home_team"]])), ag=int(float(sc[e["away_team"]])))
            except (KeyError, TypeError, ValueError):
                return None
        return None
