"""Final results for grading, from Sofascore's free scheduled-events list: who won and how many games each player won in
the whole match. A match is graded only when the feed says it is finished and it was played to the end; a retirement,
walkover, cancellation or postponement voids the pick (a tracking choice: books differ on retirements, so those picks are
left out of the win rate instead of being guessed)."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

from tennis.names import pair_score
from services.client import get_json, HttpError

log = logging.getLogger("tennis.results")
URL = "https://api.sofascore.com/api/v1/sport/tennis/scheduled-events/%s"
_VOID = ("retire", "walkover", "w.o", "cancel", "postpone", "abandon", "interrupt", "default", "disqual")


def _games(score: dict) -> int:
    return sum(int(v) for k, v in (score or {}).items() if re.fullmatch(r"period\d", str(k)) and isinstance(v, (int, float)))


def parse(payload: dict) -> list:
    out = []
    for ev in (payload or {}).get("events") or []:
        try:
            st = ev.get("status") or {}
            typ, desc = str(st.get("type") or "").lower(), str(st.get("description") or "").lower()
            hs, as_ = ev.get("homeScore") or {}, ev.get("awayScore") or {}
            hg, ag = _games(hs), _games(as_)
            code = ev.get("winnerCode")
            if code in (1, 2):
                home_won = code == 1
            else:
                hsets, asets = hs.get("current"), as_.get("current")
                home_won = (hsets > asets) if isinstance(hsets, (int, float)) and isinstance(asets, (int, float)) and hsets != asets else None
            if typ in ("canceled", "cancelled", "postponed", "abandoned") or any(w in desc for w in _VOID):
                state = "void"
            elif typ == "finished":
                state = "finished" if (home_won is not None and (hg + ag) > 0) else "other"
            else:
                state = "other"
            out.append(dict(home=(ev.get("homeTeam") or {}).get("name", ""), away=(ev.get("awayTeam") or {}).get("name", ""),
                            t=float(ev.get("startTimestamp") or 0), hg=hg, ag=ag, home_won=home_won, state=state))
        except Exception:
            continue
    return out


class Results:
    TTL = 600.0

    def __init__(self, get=None, clock=None, oa=None):
        self.oa = oa               # services.odds_api.OddsAPI: its /scores (Sofascore blocks cloud servers)
        self._oa_cache = {}
        self._get = get or get_json
        self._clock = clock or time.time
        self.cache = {}
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
        """-> {state, hg, ag, home_won} from the pick's own point of view (home = the pick's 'home' player), or None."""
        days = {datetime.fromtimestamp(x, tz=timezone.utc).strftime("%Y-%m-%d") for x in (t, t + 12 * 3600, t - 12 * 3600)}
        best = None
        for ds in sorted(days):
            for r in self.day(ds):
                if r["t"] and abs(r["t"] - t) > 14 * 3600:          # tennis start times drift a lot
                    continue
                sc, swapped = pair_score(home, away, r["home"], r["away"])
                if sc >= 0.84 and (best is None or sc > best[0]):
                    best = (sc, r, swapped)
        if not best:
            return None
        _, r, sw = best
        if not sw:
            return dict(state=r["state"], hg=r["hg"], ag=r["ag"], home_won=r["home_won"])
        return dict(state=r["state"], hg=r["ag"], ag=r["hg"], home_won=None if r["home_won"] is None else (not r["home_won"]))

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
        """Who won (sets), for a pick whose Pinnacle match came from The Odds API. The feed carries no games count and cannot
        tell a retirement, so only match-winner picks are graded from it (hg / ag are None); the games markets stay open
        for /result. None = not finished."""
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
                a, b = float(sc[e["home_team"]]), float(sc[e["away_team"]])
            except (KeyError, TypeError, ValueError):
                return None
            if a == b:
                return None
            won = a > b
            hs, as_ = (b, a) if p.get("oa_sw") else (a, b)
            return dict(state="finished", hg=None, ag=None, home_won=(not won) if p.get("oa_sw") else won, hs=int(hs), as_=int(as_))
        return None
