"""NBA injury news from ESPN's public injury feed: an advisory note on pre-match alerts and a short HOLD when the
news is fresh (Pinnacle may not have repriced yet, so the 'edge' could be an illusion).

* NOTE: who is Out / Day-To-Day for the two teams (shown on the alert).
* HOLD: a player of either team was added, changed status or cleared since our last look, less than INJURY_HOLD_MIN
  minutes ago. The first fetch after a restart only seeds the picture, so it never holds everything.
* ESPN does not say how important a player is, so this is NOT a star detector: it flags change, not impact.
* Fail-open: if ESPN is unreachable or the format changes, there is no note and no hold.
Only the NBA is covered by this feed. Env: INJURIES=0 turns it all off, INJURY_HOLD_MIN (60), INJURY_REFRESH_MIN (15).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
LEAGUES = ("nba",)
ACTIVE = ("Out", "Day-To-Day")
MAX_AGE_H = 12   # data older than this (feed down for hours) is ignored


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return os.environ.get("INJURIES", "1").strip() != "0"


def hold_min() -> float:
    return _f("INJURY_HOLD_MIN", 60.0)


def refresh_min() -> float:
    return max(1.0, _f("INJURY_REFRESH_MIN", 15.0))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fetch_espn(timeout: float = 8.0) -> dict:
    import urllib.request
    req = urllib.request.Request(ESPN_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse(payload: dict) -> Dict[str, Dict[str, str]]:
    """ESPN payload -> {team display name: {player name: status}}. Malformed entries are skipped."""
    out: Dict[str, Dict[str, str]] = {}
    for t in (payload or {}).get("injuries") or []:
        team = t.get("displayName")
        if not team:
            continue
        players = out.setdefault(str(team), {})
        for i in t.get("injuries") or []:
            name = ((i.get("athlete") or {}).get("displayName"))
            status = i.get("status")
            if name and status:
                players[str(name)] = str(status)
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def same_team(a: str, b: str) -> bool:
    """'LA Clippers' ~ 'Los Angeles Clippers': equal, one contains the other, or the same nickname (last word)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    wa, wb = str(a).split(), str(b).split()
    return bool(wa and wb and _norm(wa[-1]) == _norm(wb[-1]) and len(_norm(wa[-1])) >= 4)


class InjuryBook:
    def __init__(self, fetch: Callable[[], dict] = None, now: Callable[[], datetime] = _utcnow):
        self._fetch = fetch or fetch_espn
        self._now = now
        self.teams: Dict[str, Dict[str, str]] = {}
        self.fresh: List[dict] = []        # {team, player, status, t}
        self.fetched_at: Optional[datetime] = None
        self._tried_at: Optional[datetime] = None
        self._seeded = False

    # ---- data ----
    def refresh(self, force: bool = False) -> bool:
        """Fetch when the data is older than INJURY_REFRESH_MIN. Never raises. Returns True when new data was loaded."""
        now = self._now()
        if not force and self._tried_at is not None and (now - self._tried_at) < timedelta(minutes=refresh_min()):
            return False
        self._tried_at = now
        try:
            new = parse(self._fetch())
        except Exception:
            return False
        if not new:
            return False
        if self._seeded:
            self._diff(self.teams, new, now)
        self.teams, self.fetched_at, self._seeded = new, now, True
        keep = timedelta(minutes=max(hold_min(), 1.0) * 2)
        self.fresh = [f for f in self.fresh if now - f["t"] <= keep]
        return True

    def _diff(self, old, new, now):
        for team, players in new.items():
            before = old.get(team, {})
            for name, status in players.items():
                if before.get(name) != status:
                    self.fresh.append(dict(team=team, player=name, status=status, t=now))
            for name in before:
                if name not in players:
                    self.fresh.append(dict(team=team, player=name, status="Cleared", t=now))

    # ---- per game ----
    def _usable(self, league: str) -> bool:
        if not enabled() or league not in LEAGUES:
            return False
        self.refresh()
        if self.fetched_at is None:
            return False
        return (self._now() - self.fetched_at) <= timedelta(hours=MAX_AGE_H)

    def _team_key(self, name: str) -> Optional[str]:
        for t in self.teams:
            if same_team(name, t):
                return t
        return None

    def note(self, league: str, home: str, away: str) -> Optional[str]:
        """'Injuries: Lakers: Out A, B; day-to-day C. Kings: day-to-day D.' or None when nothing is listed."""
        if not self._usable(league):
            return None
        parts = []
        for label in (home, away):
            key = self._team_key(label)
            players = self.teams.get(key, {}) if key else {}
            out = [p for p, s in players.items() if s == "Out"]
            dtd = [p for p, s in players.items() if s == "Day-To-Day"]
            bits = []
            for tag, lst in (("Out", out), ("day-to-day", dtd)):
                if lst:
                    shown = ", ".join(lst[:3]) + (" +%d" % (len(lst) - 3) if len(lst) > 3 else "")
                    bits.append("%s %s" % (tag, shown))
            if bits:
                parts.append("%s: %s." % (label, "; ".join(bits)))
        return ("Injuries: " + " ".join(parts)) if parts else None

    def hold_reason(self, league: str, home: str, away: str) -> Optional[str]:
        """A short reason when either team had injury news in the last INJURY_HOLD_MIN minutes, else None."""
        if hold_min() <= 0 or not self._usable(league):
            return None
        now = self._now()
        win = timedelta(minutes=hold_min())
        keys = {k for k in (self._team_key(home), self._team_key(away)) if k}
        recent = [f for f in self.fresh if f["team"] in keys and now - f["t"] <= win]
        if not recent:
            return None
        f = max(recent, key=lambda x: x["t"])
        mins = int((now - f["t"]).total_seconds() // 60)
        return "injury news: %s (%s) %d min ago" % (f["player"], f["status"], mins)
