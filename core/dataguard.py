"""Data checks on Pinnacle prices BEFORE they are stored, so a wrong line or price can never become a pick.

Every market row group (market + line = one two-sided pair) must pass:
* both sides present (an orphan row is dropped),
* prices above 1.0 and not absurd,
* the pair's overround 1/a + 1/b between 0.995 and 1.15 (Pinnacle's real margin is 2-6%; outside = stale, half-priced or wrong),
* a full-game total inside 0.7x-1.4x of the league's average, a full-game spread no bigger than 0.35x of it,
  half totals inside 0.35x-0.65x, quarter totals inside 0.15x-0.35x (a half-game line filed as the full game is dropped).
Rejections are counted per day (see counts()) and shown in the 8am report. Env DATAGUARD=0 turns it off."""
from __future__ import annotations

import os
from datetime import datetime, timezone

OVERROUND_LO, OVERROUND_HI = 0.995, 1.15
_RANGES = {"": (0.70, 1.40), "h1": (0.35, 0.65), "h2": (0.35, 0.65), "q1": (0.15, 0.35), "q2": (0.15, 0.35),
           "q3": (0.15, 0.35), "q4": (0.15, 0.35)}
_SIDES = {"h2h": ("home", "away"), "spreads": ("home", "away"), "totals": ("over", "under")}


def enabled() -> bool:
    return os.environ.get("DATAGUARD", "1").strip() != "0"


def _avg_total(league):
    try:
        from config.leagues import LEAGUES
        return float(LEAGUES[league]["avg_total"])
    except Exception:
        return None


def _kind_period(mkey):
    base, _, per = str(mkey).partition("_")
    return base, per


def sanitize_event(ev: dict, league: str = None):
    """Returns (event with only the rows that passed, [reasons of what was dropped]). The input is not modified."""
    reasons = []
    out = dict(ev)
    avg = _avg_total(league or ev.get("league"))
    new = {}
    for mkey, rows in (ev.get("markets") or {}).items():
        kind, per = _kind_period(mkey)
        if kind not in _SIDES:
            new[mkey] = rows
            continue
        a, b = _SIDES[kind]
        groups = {}
        for r in rows or []:
            groups.setdefault((r.get("book"), r.get("line")), {})[r.get("selection")] = r
        keep = []
        for (book, line), sides in groups.items():
            oa, ob = sides.get(a), sides.get(b)
            if kind != "h2h" and line is None:
                reasons.append("%s: no line" % mkey)
                continue
            if oa is None or ob is None:
                reasons.append("%s %s: one side missing" % (mkey, line))
                continue
            try:
                pa, pb = float(oa["odds"]), float(ob["odds"])
            except (TypeError, ValueError, KeyError):
                reasons.append("%s %s: bad price" % (mkey, line))
                continue
            if pa <= 1.0 or pb <= 1.0 or pa > 60 or pb > 60:
                reasons.append("%s %s: price out of range" % (mkey, line))
                continue
            over = 1.0 / pa + 1.0 / pb
            if not (OVERROUND_LO <= over <= OVERROUND_HI):
                reasons.append("%s %s: overround %.3f" % (mkey, line, over))
                continue
            if avg and kind == "totals":
                lo, hi = _RANGES.get(per, (0.0, 9.9))
                if not (lo * avg <= float(line) <= hi * avg):
                    reasons.append("%s %s: total outside the league's range" % (mkey, line))
                    continue
            if avg and kind == "spreads" and per == "" and abs(float(line)) > 0.35 * avg:
                reasons.append("%s %s: spread too big" % (mkey, line))
                continue
            keep += [oa, ob]
        if keep:
            new[mkey] = keep
    out["markets"] = new
    return out, reasons


def sanitize_events(events: list, league: str, store: dict = None):
    """Whole league: returns the cleaned events (games left with no market at all are dropped) and adds today's counts to
    `store` ({'day': 'YYYY-MM-DD', 'rejected': n, 'games_dropped': n}) when given."""
    if not enabled():
        return events
    clean, n_rej, dropped = [], 0, 0
    for ev in events or []:
        e2, why = sanitize_event(ev, league)
        n_rej += len(why)
        if not e2.get("markets"):
            dropped += 1
            continue
        clean.append(e2)
    if store is not None and (n_rej or dropped):
        today = datetime.now(timezone.utc).date().isoformat()
        if store.get("day") != today:
            store.clear()
            store["day"] = today
        store["rejected"] = int(store.get("rejected", 0)) + n_rej
        store["games_dropped"] = int(store.get("games_dropped", 0)) + dropped
    return clean


def counts(store: dict) -> tuple:
    """(rejected, games dropped) of the day kept in `store`."""
    return int((store or {}).get("rejected", 0)), int((store or {}).get("games_dropped", 0))
