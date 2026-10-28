"""'Line in motion' hold for pre-match alerts.

Remembers each game's Pinnacle MAIN lines (moneyline probability, spread line, total line) every time they are
refreshed. While they have moved clearly in the last STEAM_WINDOW_MIN minutes the game is HELD: the market is still
repricing (news, sharp money), so the soft-book 'edge' is the least trustworthy then. It is released once the line has
been quiet for the whole window. This is a HOLD, not a 'follow the steam' rule: nobody has shown yet that following a
Pinnacle move pays here. The price history logged by core/pinhist.py is what will answer that.

Env: STEAM_HOLD=0 turns it off; STEAM_WINDOW_MIN (30); STEAM_H2H (0.03 = 3 probability points);
STEAM_LINE (1.0 spread points); STEAM_TOTAL (1.5 total points); jumps above STEAM_MAX_LINE (10), STEAM_MAX_TOTAL (25)
or STEAM_MAX_H2H (0.30) are treated as feed noise and ignored. Memory only: a restart starts with a clean slate."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return os.environ.get("STEAM_HOLD", "1").strip() != "0"


def window_min() -> float:
    return max(1.0, _f("STEAM_WINDOW_MIN", 30.0))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def event_key(ev: dict) -> str:
    return str(ev.get("event_id") or "%s|%s" % (ev.get("home"), ev.get("away")))


class SteamTracker:
    def __init__(self, now: Callable[[], datetime] = _utcnow, fair_fn: Callable[[dict], dict] = None):
        self._now, self._fair = now, fair_fn
        self._h: Dict[Tuple[str, str], List[Tuple[datetime, dict]]] = {}

    def record(self, league: str, events: Iterable[dict]) -> int:
        """Store a snapshot for every game whose main lines differ from the last stored one. Returns rows stored."""
        from core.pinhist import snapshot
        now, n = self._now(), 0
        keep = timedelta(minutes=window_min() * 3)
        for ev in events or []:
            try:
                snap = snapshot(ev, self._fair)
            except Exception:
                snap = None
            if not snap:
                continue
            rows = self._h.setdefault((league, event_key(ev)), [])
            if not rows or rows[-1][1] != snap:
                rows.append((now, snap))
                n += 1
            rows[:] = [r for r in rows[:-1] if now - r[0] <= keep] + rows[-1:]
        return n

    def moving(self, league: str, event_id: str) -> Optional[str]:
        """A short reason while this game's Pinnacle lines moved clearly inside the window, else None."""
        if not enabled():
            return None
        rows = self._h.get((league, str(event_id)))
        if not rows or len(rows) < 2:
            return None
        now = self._now()
        win = timedelta(minutes=window_min())
        cur_t, cur = rows[-1]
        before = [r for r in rows if r[0] <= now - win]
        base_t, base = before[-1] if before else rows[0]
        if base is cur:
            return None
        mins = max(1, int((now - base_t).total_seconds() // 60))
        why = []
        if "h2h" in cur and "h2h" in base:
            d = abs(cur["h2h"] - base["h2h"])
            if _f("STEAM_H2H", 0.03) - 1e-9 <= d <= _f("STEAM_MAX_H2H", 0.30):
                why.append("moneyline %+.0f pts" % ((cur["h2h"] - base["h2h"]) * 100))
        # A jump bigger than the MAX is not a real move: the 'main' line picked from the feed flipped to another line
        # (alternate / team / half line). Such readings are ignored instead of holding the game.
        for key, thr, cap, label in (("spread", _f("STEAM_LINE", 1.0), _f("STEAM_MAX_LINE", 10.0), "spread"),
                                     ("total", _f("STEAM_TOTAL", 1.5), _f("STEAM_MAX_TOTAL", 25.0), "total")):
            if key in cur and key in base:
                d = abs(cur[key][0] - base[key][0])
                if thr - 1e-9 <= d <= cap:
                    why.append("%s line %+g" % (label, cur[key][0] - base[key][0]))
        return ("line moving: " + ", ".join(why) + " in %d min" % mins) if why else None
