"""Remembers when each team plays, from the Pinnacle game lists the engine already fetches.

Used for the rest / back-to-back note on pre-match alerts. Pure logic: the caller passes load() and save(text)
(the engine uses the key/value table), so nothing here touches a database or the network.

Stored shape: {"<league>|<scope>": {"<team>": {"<event id>": "<ISO start, UTC>"}}}
A game that moves keeps its event id, so the new time replaces the old one. A FUTURE game that disappears from
a full refresh of its league (cancelled / postponed) is forgotten. Played games are kept for a week.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, List, Optional

KEY = "team_starts"


def _parse(v) -> Optional[datetime]:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TeamSchedule:
    def __init__(self, load: Callable[[], Optional[str]], save: Callable[[str], None],
                 now: Callable[[], datetime] = _utcnow, keep_past_days: int = 7, keep_future_days: int = 14):
        self._load, self._save, self._now = load, save, now
        self.keep_past = timedelta(days=keep_past_days)
        self.keep_future = timedelta(days=keep_future_days)
        self._d = None

    def _data(self) -> dict:
        if self._d is None:
            try:
                raw = self._load()
                d = json.loads(raw) if raw else {}
            except Exception:
                d = {}
            self._d = d if isinstance(d, dict) else {}
        return self._d

    def record(self, league: str, events: Iterable[dict], scope: str = "main") -> bool:
        """Store the start times of every event of one league refresh. Returns True when something changed."""
        d = self._data()
        bucket = d.setdefault("%s|%s" % (league, scope), {})
        now = self._now()
        lo, hi = now - self.keep_past, now + self.keep_future
        changed, seen = False, set()
        for ev in events or []:
            c = _parse(ev.get("commence"))
            home, away = ev.get("home"), ev.get("away")
            if c is None or not home or not away:
                continue
            eid = str(ev.get("event_id") or "%s|%s" % (home, away))
            seen.add(eid)
            iso = c.strftime("%Y-%m-%dT%H:%M")
            for team in (str(home), str(away)):
                games = bucket.setdefault(team, {})
                if games.get(eid) != iso:
                    games[eid] = iso
                    changed = True
        for team in list(bucket):
            games = bucket[team]
            kept = {}
            for eid, iso in games.items():
                t = _parse(iso)
                gone_future = t is not None and t > now and eid not in seen   # cancelled / postponed
                if t is not None and lo <= t <= hi and not gone_future:
                    kept[eid] = iso
            if kept != games:
                changed = True
            if kept:
                bucket[team] = kept
            else:
                bucket.pop(team, None)
        if changed:
            try:
                self._save(json.dumps(d, separators=(",", ":")))
            except Exception:
                pass
        return changed

    def starts(self, league: str, team: str) -> List[datetime]:
        out = []
        prefix = league + "|"
        for key, bucket in self._data().items():
            if not key.startswith(prefix):
                continue
            for eid, iso in (bucket.get(str(team)) or {}).items():
                t = _parse(iso)
                if t is not None:
                    out.append(t)
        return sorted(out)
