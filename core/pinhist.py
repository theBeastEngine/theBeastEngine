"""Pinnacle price history: one compact row per game whenever its main lines change.

Pure logic: the caller passes add(rows) (the engine writes them to the database). The history lets us check
closing-line value and calibration later. Only full-game moneyline, main spread and main total are kept.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable, Iterable, List, Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dt(v) -> Optional[datetime]:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def _fair(ev: dict) -> dict:
    from core.pinref import fair_probs
    return fair_probs(ev)


def snapshot(ev: dict, fair_fn: Callable[[dict], dict] = None) -> Optional[dict]:
    """{"h2h": p_home, "spread": [line, p_home], "total": [line, p_over]} from one Pinnacle event, or None."""
    fair = (fair_fn or _fair)(ev)
    snap = {}
    best = {}
    for (m, ln), sides in fair.items():
        if m == "h2h":
            if "home" in sides:
                snap["h2h"] = round(sides["home"], 4)
        elif m in ("spreads", "totals") and ln is not None:
            key = "home" if m == "spreads" else "over"
            if key not in sides:
                continue
            gap = abs(sides[key] - 0.5)
            if m not in best or gap < best[m][0]:
                best[m] = (gap, ln, sides[key])
    if "spreads" in best:
        snap["spread"] = [best["spreads"][1], round(best["spreads"][2], 4)]
    if "totals" in best:
        snap["total"] = [best["totals"][1], round(best["totals"][2], 4)]
    return snap or None


class PriceHistory:
    def __init__(self, add: Callable[[List[dict]], object], now: Callable[[], datetime] = _utcnow,
                 fair_fn: Callable[[dict], dict] = None):
        self._add, self._now, self._fair = add, now, fair_fn
        self._last = {}

    def record(self, league: str, events: Iterable[dict]) -> int:
        """Log every game whose snapshot differs from the last one logged. Returns the number of rows written."""
        rows, ts = [], self._now()
        for ev in events or []:
            try:
                snap = snapshot(ev, self._fair)
            except Exception:
                snap = None
            if not snap:
                continue
            key = (league, str(ev.get("event_id") or "%s|%s" % (ev.get("home"), ev.get("away"))))
            if self._last.get(key) == snap:
                continue
            self._last[key] = snap
            rows.append(dict(ts=ts, league=league, event_id=key[1], home=str(ev.get("home") or "")[:80],
                             away=str(ev.get("away") or "")[:80], commence=_dt(ev.get("commence")),
                             snap=json.dumps(snap, separators=(",", ":"))))
        if rows:
            self._add(rows)
        return len(rows)


def closing_prob(pick: dict, rows: Iterable[dict], same: Callable[[str, str], bool] = None) -> Optional[float]:
    """Closing FAIR probability of the pick's side: the last logged snapshot taken before the game started, only when
    its line equals the pick's line. None when unknown (other markets, no history, line moved)."""
    if same is None:
        from core.pinref import _same as same
    market, sel = pick.get("market"), pick.get("selection")
    if market not in ("h2h", "spreads", "totals"):
        return None
    start = _dt(pick.get("commence"))
    best = None
    for r in rows:
        if not (same(str(pick.get("home")), r["home"]) and same(str(pick.get("away")), r["away"])):
            continue
        t0 = _dt(r.get("commence")) or start
        if t0 is not None and r["ts"] > t0:
            continue
        if best is None or r["ts"] > best["ts"]:
            best = r
    if best is None:
        return None
    try:
        snap = json.loads(best["snap"])
    except Exception:
        return None
    if market == "h2h":
        p = snap.get("h2h")
    elif market == "spreads":
        v = snap.get("spread")
        if not v or pick.get("line") is None or abs(float(v[0]) - float(pick["line"])) > 1e-6:
            return None
        p = v[1]
    else:
        v = snap.get("total")
        if not v or pick.get("line") is None or abs(float(v[0]) - float(pick["line"])) > 1e-6:
            return None
        p = v[1]
    if p is None:
        return None
    return p if sel in ("home", "over") else 1.0 - p
