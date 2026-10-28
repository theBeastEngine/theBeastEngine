"""Coverage check for a sport we may add: how many SportyBet games there are, whether OddsPapi carries the same leagues with
Pinnacle prices, and which Pinnacle markets exist. Costs about 2 OddsPapi calls per sport plus 2 shared ones (sports list, market
catalog). Read-only: nothing is stored and no bet is ever made from it."""
from __future__ import annotations

import logging
import re
import time
from collections import Counter

log = logging.getLogger("coverage")

SPORTS = [("Ice hockey", "sr:sport:4", ("ice hockey", "hockey")),
          ("American football", "sr:sport:16", ("american football",)),
          ("Volleyball", "sr:sport:23", ("volleyball",)),
          ("Handball", "sr:sport:6", ("handball",))]
URL = "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents"
HEADERS = {"operid": "2", "clientid": "web", "platform": "web", "Referer": "https://www.sportybet.com/ng/"}
# SportyBet refuses an empty marketId list (HTTP 422 "Invalid"); the tennis feed only works with explicit ids. 1/16/18/11 = three-way result,
# Asian handicap, over/under, draw no bet; 219/223/225 = winner / handicap / total including overtime; 186-200 = the set-sport family.
IDS_ALL = "1,11,16,18,219,223,225," + ",".join(str(i) for i in range(186, 201))
NOISE = {"league", "division", "season", "regular", "play", "offs", "playoffs", "women", "men", "womens", "mens", "cup", "championship",
         "ice", "hockey", "volleyball", "handball", "american", "football", "international", "professional", "first", "second",
         "the", "and", "usa", "world"}


def _tokens(s: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) >= 3 and t not in NOISE}


def sporty_scan(get, sport_id: str, horizon_h: float = 48, now: float = None, pages: int = 3) -> dict:
    """-> dict(games, leagues Counter(name -> games), markets Counter(market name -> games), error)."""
    now = now if now is not None else time.time()
    out = dict(games=0, leagues=Counter(), markets=Counter(), error=None)
    for page in range(1, pages + 1):
        payload = None
        for shape in (dict(marketId=IDS_ALL, pageSize=50), dict(marketId="1,16,18,11", pageSize=50), dict(marketId="219,223,225", pageSize=50),
                      dict(marketId=",".join(str(i) for i in range(186, 201)), pageSize=50)):
            try:
                payload, _ = get(URL, dict(sportId=sport_id, pageNum=page, todayGames="false", timeline="", **shape), HEADERS, 20, 2)
                break
            except Exception as e:                     # a shape SportyBet refuses (422) -> try the next one
                out["error"] = str(e)[:100]
        if payload is None:
            break
        data = (payload or {}).get("data")
        tours = data.get("tournaments") if isinstance(data, dict) else data
        if not tours:
            break
        out["error"] = None
        for t in tours:
            if not isinstance(t, dict):
                continue
            lname = ("%s - %s" % (t.get("categoryName"), t.get("name"))) if t.get("categoryName") else str(t.get("name") or "")
            for ev in t.get("events") or []:
                try:
                    ms = float(ev.get("estimateStartTime"))
                    ts = ms / 1000.0 if ms > 1e11 else ms
                except (TypeError, ValueError):
                    continue
                if not (ev.get("homeTeamName") and ev.get("awayTeamName")) or ts < now - 3600 or ts > now + horizon_h * 3600:
                    continue
                out["games"] += 1
                out["leagues"][lname] += 1
                for nm in {str(m.get("desc") or m.get("name") or "").strip() for m in ev.get("markets") or [] if isinstance(m, dict)}:
                    if nm:
                        out["markets"][nm] += 1
    return out


def match_tournaments(leagues: list, rows: list) -> dict:
    """{SportyBet league name: (tournamentId, name)} by the best overlap of distinctive words; leagues without a match are omitted."""
    found = {}
    for ln in leagues:
        mine = _tokens(ln)
        best, score = None, 0
        for r in rows or []:
            tid = r.get("tournamentId", r.get("id"))
            nm = str(r.get("tournamentName", r.get("name", "")))
            sc = len(mine & _tokens("%s %s" % (nm, r.get("categoryName", ""))))
            if sc > score:
                best, score = (tid, nm), sc
        if best and score:
            found[ln] = best
    return found


def pinnacle_scan(papi, kws: tuple, top_leagues: list, cat_by_id: dict, sports_rows) -> dict:
    """-> dict(sport_id, matched {league: (tid, name)}, fixtures n, per_tournament Counter, markets Counter, error)."""
    out = dict(sport_id=None, matched={}, fixtures=0, per_tid=Counter(), markets=Counter(), error=None)
    rows_s = [r for r in (sports_rows if isinstance(sports_rows, list) else []) if isinstance(r, dict)]
    for k in kws:                                     # exact name first, then a name that contains it (never field hockey / table tennis)
        for r in rows_s:
            nm = str(r.get("sportName", r.get("name", ""))).lower().strip()
            if nm == k or (k in nm and "field" not in nm and "table" not in nm and "beach" not in nm):
                out["sport_id"] = r.get("sportId", r.get("id"))
                break
        if out["sport_id"] is not None:
            break
    if out["sport_id"] is None:
        out["error"] = "OddsPapi has no such sport"
        return out
    rows = papi.call("/v4/tournaments", {"sportId": out["sport_id"], "language": "en"})
    out["matched"] = match_tournaments(top_leagues, rows if isinstance(rows, list) else [])
    tids = []
    for _, (tid, _) in out["matched"].items():
        if tid not in tids:
            tids.append(tid)
    if not tids:
        return out
    try:
        fx = papi.call("/v4/odds-by-tournaments", {"bookmaker": "pinnacle", "tournamentIds": ",".join(str(t) for t in tids[:4])})
    except Exception as e:
        out["error"] = str(e)[:90]
        return out
    for f in fx if isinstance(fx, list) else []:
        mk = (((f.get("bookmakerOdds") or {}).get("pinnacle") or {}).get("markets")) or {}
        if not mk:
            continue
        out["fixtures"] += 1
        out["per_tid"][f.get("tournamentId")] += 1
        seen = set()
        for mid in mk:
            try:
                c = cat_by_id.get(int(mid)) or {}
            except (TypeError, ValueError):
                c = {}
            seen.add(str(c.get("marketType") or "?") + (" " + str(c.get("period")) if c.get("period") not in (None, "fulltime", "full") else ""))
        out["markets"].update(seen)
    return out


def verdict(sb: dict, pn: dict) -> str:
    if sb.get("error") and not sb.get("games"):
        return "NOT CHECKED (SportyBet refused the request)"
    if pn.get("error") and not pn.get("fixtures"):
        return "NOT CHECKED (%s)" % pn["error"]
    n, g = pn.get("fixtures", 0), sb.get("games", 0)
    kinds = " ".join(pn.get("markets", {})).lower()
    core = any(k in kinds for k in ("moneyline", "h2h", "1x2", "winner")) and any(k in kinds for k in ("total", "spread", "handicap"))
    if g < 10:
        return "WEAK: few SportyBet games right now (%d)" % g
    if n >= 8 and core:
        return "GOOD FIT"
    if n >= 3:
        return "POSSIBLE: only %d Pinnacle fixtures in the leagues tried" % n
    return "POOR: Pinnacle prices almost none of the top SportyBet leagues"


def run(papi, get, sports=SPORTS, now: float = None) -> list:
    """-> list of plain-text lines, one block per sport."""
    sports_rows = papi.call("/v4/sports", {"language": "en"})
    cat = papi.call("/v4/markets", {"language": "en"})
    cat_by_id = {}
    for m in cat if isinstance(cat, list) else []:
        try:
            cat_by_id[int(m.get("marketId"))] = m
        except (TypeError, ValueError):
            continue
    lines = []
    for label, sid, kws in sports:
        sb = sporty_scan(get, sid, now=now)
        top = [k for k, _ in sb["leagues"].most_common(6)]
        try:
            pn = pinnacle_scan(papi, kws, top, cat_by_id, sports_rows)
        except Exception as e:
            log.warning("coverage %s failed: %s", label, e)
            pn = dict(sport_id=None, matched={}, fixtures=0, per_tid=Counter(), markets=Counter(), error=str(e)[:90])
        lines.append("%s: %s" % (label.upper(), verdict(sb, pn)))
        if sb["error"] and not sb["games"]:
            lines.append("  SportyBet: could not read (%s)" % sb["error"])
        else:
            lines.append("  SportyBet: %d games in the next 48h, %d leagues" % (sb["games"], len(sb["leagues"])))
            lines.append("  Top leagues: " + "; ".join("%s %d" % (k[:34], v) for k, v in sb["leagues"].most_common(5)))
            lines.append("  Markets: " + ", ".join("%s %d" % (k[:22], v) for k, v in sb["markets"].most_common(6)))
        if pn["sport_id"] is not None:
            lines.append("  Pinnacle (OddsPapi): %d of the top %d SportyBet leagues found, %d fixtures with a Pinnacle price" % (
                len(pn["matched"]), len(top), pn["fixtures"]))
            if pn["markets"]:
                lines.append("  Pinnacle markets: " + ", ".join("%s %d" % (k, v) for k, v in pn["markets"].most_common(7)))
            miss = [k for k in top if k not in pn["matched"]]
            if miss:
                lines.append("  No OddsPapi league for: " + "; ".join(k[:30] for k in miss[:3]))
        lines.append("")
    return lines
