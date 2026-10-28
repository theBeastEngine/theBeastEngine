"""Final scores of every game the engine sees live, for grading the leagues no score API covers.

The live scan already reads every live game on SportyBet (score, period, clock) every ~2 minutes. This module remembers
the LAST thing seen of each game. A game that has left the live feed, last seen in its final period with little time left,
is finished: its last score (plus what the remaining seconds could still add) grades the picks.
A pick is graded ONLY when the result is the same in every way the last seconds could have gone (nobody can still
cover / go over / win), and never when the game could have gone to overtime. Anything unsure stays ungraded.
Env: FINALS=0 turns it off, FINALS_GAP_SEC (480) how long a game must be missing before it counts as finished."""
from __future__ import annotations

import json
import math
import os
import threading
import time

from services.logger import get_logger

log = get_logger("finals")
KEY = "live_finals"
MAX_REM = 300          # last seen with at most this many seconds left in the final period
KEEP_DONE_DAYS = 6


def enabled() -> bool:
    return os.environ.get("FINALS", "1").strip() != "0"


def _gap() -> float:
    try:
        return float(os.environ.get("FINALS_GAP_SEC", 480))
    except ValueError:
        return 480.0


def _key(league, home, away) -> str:
    return "%s|%s|%s" % (league, home, away)


def extra_points(rem: float) -> int:
    """Most points ONE team can still score in `rem` seconds (generous: threes and late free throws)."""
    return int(3 * math.ceil(max(rem, 0) / 60.0) + 4) if rem > 0 else 0


class Finals:
    def __init__(self, load=None, save=None, now=None):
        self._load, self._save = load, save
        self._now = now or time.time
        self._lock = threading.RLock()
        self.last, self.done = None, None
        self._saved = None

    # ---- state
    def _ensure(self):
        if self.last is None:
            blob = None
            try:
                blob = self._load() if self._load else None
                d = json.loads(blob) if blob else {}
            except Exception:
                d = {}
            self.last, self.done = d.get("last") or {}, d.get("done") or {}

    def flush(self):
        with self._lock:
            self._ensure()
            try:
                s = json.dumps(dict(last=self.last, done=self.done), separators=(",", ":"))
                if self._save and s != self._saved:
                    self._save(s)
                    self._saved = s
            except Exception as e:
                log.warning("finals not saved: %s", e)

    # ---- fed by the live scan (cheap, memory only)
    def observe(self, games: list):
        if not enabled():
            return
        now = self._now()
        with self._lock:
            self._ensure()
            for g in games or []:
                try:
                    lg, h, a = g["league"], g["home"], g["away"]
                    try:
                        from core.bookmakers.sportybet import _fmt_for
                        n = int(_fmt_for(lg)[0])
                    except Exception:
                        n = 4
                    k = _key(lg, h, a)
                    old = self.last.get(k) or {}
                    per = g.get("break_after") if g.get("halftime") else g.get("period")
                    per = int(per or old.get("period") or 0)
                    self.last[k] = dict(league=lg, home=h, away=a, hs=int(g["home_score"]), as_=int(g["away_score"]),
                                        period=per, n=n, rem=float(g.get("remaining") or 0) if not g.get("halftime") else 0.0,
                                        ts=now, first=old.get("first") or now)
                except Exception:
                    continue

    def closing(self) -> bool:
        """True while some live game is in its last period with at most CLOSING_SEC (420) seconds left: the engine then
        looks at the live feed every 30 s so the last score it keeps is within seconds of the final whistle."""
        if not enabled():
            return False
        now, lim = self._now(), float(os.environ.get("CLOSING_SEC", 420))
        with self._lock:
            self._ensure()
            return any(g["period"] >= g["n"] and g["rem"] <= lim and now - g["ts"] < 600 for g in self.last.values())

    # ---- finish detection + persistence (called every few minutes)
    def tick(self) -> int:
        if not enabled():
            return 0
        now = self._now()
        n_new = 0
        with self._lock:
            self._ensure()
            for k, s in list(self.last.items()):
                age = now - s["ts"]
                if age >= _gap() and s["period"] >= s["n"] and s["rem"] <= MAX_REM:
                    self.done[k] = dict(s, finished=now)
                    del self.last[k]
                    n_new += 1
                elif age > 6 * 3600:
                    del self.last[k]                        # vanished mid-game (feed gap): nothing to grade from
            for k in [k for k, d in self.done.items() if now - d["finished"] > KEEP_DONE_DAYS * 86400]:
                del self.done[k]
        self.flush()
        if n_new:
            log.info("finals: %d game(s) finished", n_new)
        return n_new

    # ---- lookup for the grader
    def lookup(self, league, home, away, commence_ts):
        from core.pinref import _same
        with self._lock:
            self._ensure()
            for d in self.done.values():
                if d["league"] != league or not (_same(home, d["home"]) and _same(away, d["away"])):
                    continue
                if commence_ts is not None and not (commence_ts - 3600 <= d["first"] <= commence_ts + 8 * 3600):
                    continue
                return d
        return None


def grade(pick: dict, fin: dict):
    """('won'|'lost'|'void', (home, away)) or None. Same answer for every way the unseen seconds could have gone."""
    from core.grader import grade_pick
    x = extra_points(fin.get("rem", 0))
    results = set()
    for dh, da in {(0, 0), (x, 0), (0, x), (x, x)}:
        h, a = fin["hs"] + dh, fin["as_"] + da
        if h == a:
            return None                                       # a tie: overtime possible
        r = grade_pick(pick, h, a)
        if r is None:
            return None
        results.add(r)
    if len(results) != 1:
        return None
    return results.pop(), (fin["hs"], fin["as_"])


_inst = None


def shared(load=None, save=None) -> Finals:
    global _inst
    if _inst is None:
        _inst = Finals(load, save)
    return _inst
