"""Which free public score feeds can the server reach, and what do they return for yesterday's games? Read-only diagnostic for choosing
a result source for grading (Sofascore answers 403 from cloud servers, OddsPapi's settlement feed refuses the free keys). No API key is
used and nothing is stored."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("sources")

ESPN = "https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard"
ESPN_FEEDS = [("ESPN football England", "soccer/eng.1"), ("ESPN football Spain", "soccer/esp.1"), ("ESPN football Germany", "soccer/ger.1"),
              ("ESPN football Brazil", "soccer/bra.1"), ("ESPN tennis ATP", "tennis/atp"), ("ESPN tennis WTA", "tennis/wta"),
              ("ESPN ice hockey NHL", "hockey/nhl"), ("ESPN American football NFL", "football/nfl")]
SPORTSDB = "https://www.thesportsdb.com/api/v1/json/3/eventsday.php"
SPORTSDB_FEEDS = [("TheSportsDB football", "Soccer"), ("TheSportsDB tennis", "Tennis"), ("TheSportsDB ice hockey", "Ice Hockey"),
                  ("TheSportsDB handball", "Handball"), ("TheSportsDB volleyball", "Volleyball")]


def _espn_summary(payload) -> str:
    evs = (payload or {}).get("events") or []
    done = 0
    sample = ""
    for e in evs:
        comp = ((e.get("competitions") or [{}])[0]) if isinstance(e, dict) else {}
        st = ((e.get("status") or {}).get("type") or {})
        if st.get("completed"):
            done += 1
            if not sample:
                cs = comp.get("competitors") or []
                sample = " | ".join("%s %s" % (((c.get("team") or c.get("athlete") or {}).get("displayName") or "?"), c.get("score", "?")) for c in cs[:2])
    return "%d games, %d finished%s" % (len(evs), done, (", e.g. " + sample[:70]) if sample else "")


def _sportsdb_summary(payload) -> str:
    evs = (payload or {}).get("events") or []
    scored = [e for e in evs if isinstance(e, dict) and e.get("intHomeScore") not in (None, "")]
    s = scored[0] if scored else None
    return "%d games, %d with a final score%s" % (len(evs), len(scored),
                                                 (", e.g. %s %s-%s %s" % (str(s.get("strHomeTeam"))[:22], s.get("intHomeScore"), s.get("intAwayScore"), str(s.get("strAwayTeam"))[:22])) if s else "")


def probe(get, now: float = None) -> list:
    """-> plain-text lines, one per feed: reachable or the error, and what yesterday's results look like."""
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    day = datetime.fromtimestamp(now - 86400, tz=timezone.utc)
    lines = ["Results for %s" % day.strftime("%Y-%m-%d")]
    for label, path in ESPN_FEEDS:
        try:
            payload, _ = get(ESPN % path, {"dates": day.strftime("%Y%m%d")}, {"Accept": "application/json"}, 15, 1)
            lines.append("OK   %s: %s" % (label, _espn_summary(payload)))
        except Exception as e:
            lines.append("FAIL %s: %s" % (label, str(e)[:70]))
    for label, sport in SPORTSDB_FEEDS:
        try:
            payload, _ = get(SPORTSDB, {"d": day.strftime("%Y-%m-%d"), "s": sport}, {"Accept": "application/json"}, 15, 1)
            lines.append("OK   %s: %s" % (label, _sportsdb_summary(payload)))
        except Exception as e:
            lines.append("FAIL %s: %s" % (label, str(e)[:70]))
    return lines
