# core/bookmakers/_base.py
"""Shared helpers for the native soft-book adapters (0 Odds API credits).

Every adapter exposes:  BOOK  and  async fetch_odds(league_id=None) -> list[row]
Rows are built ONLY through core.bookmakers.make_row (via build_rows below).
Markets produced: ML, Spread, Total (full game, incl. OT), one main line each.
"""
import asyncio
import difflib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.bookmakers import make_row
from services.logger import get_logger

logger = get_logger("bookmakers")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# CLAUDE FIX: SportyBet's "LNB" tournaments also exist for Chile and Argentina (LNB = their national leagues);
# those games were being tagged "lnb" and compared with French prices.
_LATAM = ["chile", "chilean", "argentina", "argentine", "uruguay", "venezuela", "colombia", "peru",
          "paraguay", "bolivia", "brazil", "ecuador", "mexico"]

# league_id -> (include combos [OR of AND-lists], exclude terms). Matched on "country + league name".
LEAGUE_RULES: Dict[str, Tuple[List[List[str]], List[str]]] = {
    "nba": ([["nba"]], ["wnba", "g league", "g-league", "summer league", "2k"]),
    "g_league": ([["g league"], ["g-league"], ["nba g"]], []),
    "euroleague": ([["euroleague"]], ["women", "eurocup"]),
    "acb": ([["acb"], ["liga endesa"], ["spain", "liga"]], ["women", "feb"]),
    "bsl": ([["bsl"], ["turkey", "super"], ["turkiye", "super"], ["turkish", "super"]], ["women", "kadin"]),   # SportyBet writes "Turkiye - Super Lig"
    "bbl": ([["germany", "bbl"], ["germany", "bundesliga"]], ["women", "pro a", "pro b"]),
    "lnb": ([["lnb"], ["betclic elite"], ["france", "pro a"], ["france", "elite"]],
            ["women", "pro b", "elite 2", "espoirs", "nm1"] + _LATAM),
    "ncaab": ([["ncaa"], ["college"]], ["women", "ncaaw", "philippines", "seniors", "juniors"]),   # PH "NCAA, Seniors" is a different league
    "nbl": ([["australia", "nbl"]], ["women", "nbl1"]),
    "cba": ([["china", "cba"], ["cba"]], ["women"]),
    # SportyBet (Oct 2026) writes "Japan - B League Premier" (no dot); B League One / Next are the lower divisions
    "japan_b1": ([["b.league"], ["japan", "b1"], ["japan", "b.league"], ["japan", "b league", "premier"]],
                 ["women", "b2", "b3", "one", "next", "w league"]),
    "kbl": ([["korea", "kbl"], ["kbl"]], ["women", "wkbl"]),
    "pba": ([["philippines", "pba"], ["pba"]], ["women"]),
    "bal": ([["basketball africa league"], ["bal"]], ["women"]),
}
# CLAUDE FIX: women's competitions in other languages ("Liga Femenina" matched the ACB rule via spain+liga)
_BAD = ("women", "womens", "femenina", "femenino", "feminina", "feminino", "femenil", "feminine", "feminin",
        "femminile", "frauen", "damen", "dames", "virtual", "esoccer", "cyber", "u19", "u21", "u23", "youth")


def _ascii(s: str) -> str:
    """Strip accents so 'F\u00e9minine' = 'feminine' and 'S\u00fcper' = 'super'."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _has(text: str, term: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None


def league_matches(text: str, league_id: str) -> bool:
    text = _ascii((text or "").lower())
    rule = LEAGUE_RULES.get(league_id)
    if not rule or any(_has(text, b) for b in _BAD):
        return False
    inc, exc = rule
    if any(_has(text, x) for x in exc):
        return False
    return any(all(_has(text, t) for t in combo) for combo in inc)


def infer_league(text: str) -> Optional[str]:
    return next((lid for lid in LEAGUE_RULES if league_matches(text, lid)), None)


def wanted(text: str, league_id: Optional[str]) -> Optional[str]:
    """League id if this competition is wanted, else None."""
    if league_id:
        return league_id if league_matches(text, league_id) else None
    return infer_league(text)


def _norm_name(s) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(s or "").lower().replace("-", " ")).strip()


def same_team(a, b) -> bool:
    """Loose team-name match (LA Lakers = Los Angeles Lakers). Same-city rivals must NOT match."""
    x, y = _norm_name(a), _norm_name(b)
    if not x or not y:
        return False
    if x == y:
        return True
    cx, cy = x.replace(" ", ""), y.replace(" ", "")
    if len(cx) >= 5 and len(cy) >= 5 and (cx in cy or cy in cx):
        return True
    lx, ly = x.split()[-1], y.split()[-1]
    if lx == ly and len(lx) >= 4:
        return True
    return difflib.SequenceMatcher(None, cx, cy).ratio() >= 0.85


def parse_clock(s) -> Optional[int]:
    """'29:25' -> 1765 seconds ('1:02:03' also ok); None when it is not a clock."""
    try:
        parts = [int(x) for x in str(s).strip().split(":")]
    except ValueError:
        return None
    if len(parts) == 2 and parts[1] < 60:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def to_iso(ts) -> Optional[str]:
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return None
    if v > 1e11:          # milliseconds
        v /= 1000.0
    return datetime.fromtimestamp(v, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def fnum(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def pair_candidates(a: List[Tuple[float, float]], b: List[Tuple[float, float]],
                    negate: bool = False) -> List[Tuple[float, float, float]]:
    """Join two sides listed as (line, odds) into (line_of_a, odds_a, odds_b).
    negate=True for handicaps, where side b's line is -line of side a."""
    bmap = {round(-ln if negate else ln, 2): o for ln, o in b}
    return [(ln, o, bmap[round(ln, 2)]) for ln, o in a if round(ln, 2) in bmap]


def most_balanced(cands: List[Tuple[float, float, float]]) -> Optional[Tuple[float, float, float]]:
    """Main line = the line whose two prices are closest together."""
    ok = [c for c in cands if c[1] and c[2] and c[1] > 1.01 and c[2] > 1.01]
    return min(ok, key=lambda c: abs(c[1] - c[2])) if ok else None


def build_rows(book: str, league: str, ext_id, home: str, away: str, commence: Optional[str],
               ml: Optional[Tuple[float, float]] = None,
               spread: Optional[Tuple[float, float, float]] = None,
               total: Optional[Tuple[float, float, float]] = None,
               periods: Optional[Dict] = None) -> List[Dict]:
    """ml=(home, away); spread=(home_line, home_odds, away_odds); total=(line, over, under).
    periods={"q1": {"spread": (...), "total": (...)}, "h1": {...}} adds quarter / half rows (Spread_q1, Total_h1 ...)."""
    if not (home and away and ext_id is not None and league):
        return []
    mid = f"{book}:{ext_id}"
    extra = {"league": league, "home": home, "away": away, "commence": commence, "ext_id": str(ext_id)}
    rows: List[Dict] = []

    def add(market, side, odds, line):
        r = make_row(mid, market, side, odds, line, book, **extra)
        if r:
            rows.append(r)

    if ml:
        add("ML", "home", ml[0], None)
        add("ML", "away", ml[1], None)
    if spread:
        add("Spread", "home", spread[1], spread[0])
        add("Spread", "away", spread[2], -spread[0])      # each side carries its OWN line
    if total:
        add("Total", "over", total[1], total[0])
        add("Total", "under", total[2], total[0])
    for per, d in sorted((periods or {}).items()):
        sp, tt = (d or {}).get("spread"), (d or {}).get("total")
        if sp:
            add("Spread_" + per, "home", sp[1], sp[0])
            add("Spread_" + per, "away", sp[2], -sp[0])
        if tt:
            add("Total_" + per, "over", tt[1], tt[0])
            add("Total_" + per, "under", tt[2], tt[0])
    return rows


_BLOCK_URL = re.compile(r"/block(?:[/?#]|$)")


async def get_json(url: str, *, params=None, headers=None, timeout: float = 15.0, retries: int = 2):
    """GET -> parsed JSON, or None. Never raises. Does not hammer on 403/429, block pages or HTML replies."""
    try:
        import httpx
    except ImportError:
        logger.error("httpx is not installed")
        return None
    hdr = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
                r = await c.get(url, params=params, headers=hdr)
            # CLAUDE FIX: geo-blocked books (1xBet, Paripesa) redirect to /en/block and answer 200 with HTML;
            # that used to be retried 3 times (~5 s wasted per call). Not retryable -> stop at once.
            if _BLOCK_URL.search(str(r.url).lower()):
                logger.warning("GET %s -> redirected to a block page (%s); geo/IP blocked", url, r.url)
                return None
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    logger.warning("GET %s -> HTTP 200 but not JSON (%s)", url, r.headers.get("content-type", "?"))
                    return None
            logger.warning("GET %s -> HTTP %s", url, r.status_code)
            if r.status_code in (401, 403, 404, 429):
                return None
        except Exception as e:  # network errors
            logger.warning("GET %s failed: %s", url, e)
        await asyncio.sleep(0.8 * (attempt + 1))
    return None


_shape_logged: set = set()


def log_shape_once(book: str, payload: Any) -> None:
    """Log the response structure once per book so parsers can be corrected from real data."""
    if book in _shape_logged:
        return
    _shape_logged.add(book)

    def shape(x, d=0):
        if d > 3:
            return "..."
        if isinstance(x, dict):
            return {k: shape(v, d + 1) for k, v in list(x.items())[:12]}
        if isinstance(x, list):
            return [shape(x[0], d + 1)] if x else []
        return type(x).__name__
    logger.info("%s response shape: %s", book, json.dumps(shape(payload))[:1500])


_captured: set = set()


def save_capture(book: str, payload: Any) -> None:
    """Save ONE raw response per book per process to data/captures/<book>.json (for writing/fixing a parser).
    CLAUDE FIX: it used to rewrite the file on every sweep and cut the JSON at 300k characters, which left
    an unparseable file for big feeds. Now: once per process, complete JSON, skipped above 8 MB.
    Set BEAST_CAPTURE=0 to switch it off."""
    if book in _captured or os.getenv("BEAST_CAPTURE", "1") == "0":
        return
    _captured.add(book)
    try:
        text = json.dumps(payload)
        if len(text) > 8_000_000:
            logger.debug("capture for %s too large (%d chars), skipped", book, len(text))
            return
        d = os.path.join(os.getenv("BEAST_DATA_DIR", "data"), "captures")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{book}.json"), "w") as f:
            f.write(text)
    except Exception as e:
        logger.debug("capture not saved: %s", e)


# --- urllib transport -------------------------------------------------------------------------------
# SportyBet answers httpx requests with HTTP 202 and an empty body, but the same request through urllib gets 200.
# This definition replaces the httpx-based get_json above (same contract: JSON or None, never raises).
def _fetch_sync(url, params, hdr, timeout):
    import urllib.error
    import urllib.parse
    import urllib.request
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=timeout) as r:
            return r.status, r.geturl(), r.headers.get("content-type", "?"), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, "", ""


async def get_json(url: str, *, params=None, headers=None, timeout: float = 15.0, retries: int = 2):
    """GET -> parsed JSON, or None. Never raises. Does not hammer on 403/429, block pages or HTML replies."""
    import json
    hdr = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    for attempt in range(retries + 1):
        try:
            status, final_url, ctype, body = await asyncio.to_thread(_fetch_sync, url, params, hdr, timeout)
            if _BLOCK_URL.search(str(final_url).lower()):
                logger.warning("GET %s -> redirected to a block page (%s); geo/IP blocked", url, final_url)
                return None
            if status == 200:
                try:
                    return json.loads(body)
                except ValueError:
                    logger.warning("GET %s -> HTTP 200 but not JSON (%s)", url, ctype)
                    return None
            logger.warning("GET %s -> HTTP %s", url, status)
            if status in (401, 403, 404, 429):
                return None
        except Exception as e:
            logger.warning("GET %s failed: %s", url, e)
        await asyncio.sleep(0.8 * (attempt + 1))
    return None
