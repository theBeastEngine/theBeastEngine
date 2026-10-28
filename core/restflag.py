"""Rest / back-to-back flag from game start times we already see.

Pure functions, no I/O. `starts` is every known start time for ONE team
(datetime, naive UTC or all tz-aware). The flag is a note on the alert,
not a hard block: Pinnacle already prices rest.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, List, Optional

B2B_HOURS = 30      # previous game started less than 30 h before tip-off
WINDOW_DAYS = 4     # look back 4 days for "3rd game in 4 nights"


def _before(starts: Iterable[datetime], target: datetime) -> List[datetime]:
    return sorted(s for s in starts if s < target and (target - s) <= timedelta(days=WINDOW_DAYS))


def rest_info(starts: Iterable[datetime], target: datetime) -> dict:
    prev = _before(starts, target)
    if not prev:
        return {"rest_days": None, "b2b": False, "third_in_four": False, "games_last4": 0}
    gap = target - prev[-1]
    b2b = gap < timedelta(hours=B2B_HOURS)
    return {
        "rest_days": max(0, gap.days),
        "b2b": b2b,
        "third_in_four": len(prev) >= 2,
        "games_last4": len(prev),
    }


def rest_flag(home_starts, away_starts, target: datetime, home: str, away: str) -> Optional[str]:
    """One plain-English line for the alert, or None when nothing is notable."""
    h = rest_info(home_starts, target)
    a = rest_info(away_starts, target)
    notes = []
    for name, i in ((home, h), (away, a)):
        if i["b2b"]:
            notes.append(f"{name} on a back-to-back")
        elif i["third_in_four"]:
            notes.append(f"{name} playing a 3rd game in 4 days")
    if (
        h["rest_days"] is not None and a["rest_days"] is not None
        and abs(h["rest_days"] - a["rest_days"]) >= 2
    ):
        fresher = home if h["rest_days"] > a["rest_days"] else away
        notes.append(f"{fresher} has {abs(h['rest_days'] - a['rest_days'])} more rest days")
    return "Rest: " + "; ".join(notes) if notes else None
