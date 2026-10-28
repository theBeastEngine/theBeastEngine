"""Free live scores from Sofascore's unofficial API (field names may change). Real games only."""
import asyncio
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from services.client import get_json, HttpError
from services.logger import get_logger

log = get_logger("sofascore")
URL = "https://api.sofascore.com/api/v1/sport/basketball/events/live"
FAKE = ("esoccer", "esports", "e-basketball", "ebasketball", "cyber", "virtual", "simulated", "2k", "fifa", "nba2k")
# order matters: specific before generic
LEAGUE_PATTERNS = [
    ("g_league", r"g league"), ("nba", r"^nba\b"), ("euroleague", r"euroleague"), ("acb", r"\bacb\b|liga endesa"),
    ("bsl", r"\bbsl\b|super ?lig|basketbol s"), ("bbl", r"\bbbl\b|basketball bundesliga"),
    ("lnb", r"\blnb\b|betclic elite|\bpro a\b"), ("ncaab", r"ncaa"), ("nbl", r"\bnbl\b"), ("cba", r"\bcba\b"),
    ("japan_b1", r"b\.?league|japan b1"), ("kbl", r"\bkbl\b"), ("pba", r"\bpba\b"),
    ("bal", r"basketball africa league|\bbal\b"),
]
# CLAUDE FIX (same problem as the SportyBet feed): "LNB" is also Chile's league, "Superliga" is Serbia's, a
# bare "Basketball Bundesliga" matches the 2nd division, B.League 2/3 matched japan_b1, etc.
WOMEN = ("women", "wnba", "femenina", "femenino", "feminina", "feminino", "femenil", "feminine", "feminin",
         "femminile", "frauen", "damen", "ladies", "dames")


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
EXCLUDE = {
    "nba": r"summer league",
    "lnb": r"pro b|elite 2|espoirs|nm1|nm2",
    "bbl": r"2\.|pro ?[ab]\b|regionalliga",
    "japan_b1": r"b\.? ?league ?[23]|\bb[23]\b",
    "acb": r"\bleb\b|primera feb|segunda feb",
    "ncaab": r"seniors|juniors|philippines",       # Philippine NCAA is not US college basketball
}
# when Sofascore gives the category (country), the league must be in it; no category = no check
COUNTRY = {"lnb": ("france",), "bsl": ("turk", "t\u00fcrk"), "bbl": ("germany",), "acb": ("spain",),
           "nbl": ("australia",), "cba": ("china",), "kbl": ("korea",), "pba": ("philippines",),
           "japan_b1": ("japan",)}


def league_of(tournament: str, category: str = ""):
    t, cat = _ascii((tournament or "").lower()), _ascii((category or "").lower())
    if any(w in t or w in cat for w in WOMEN):
        return None
    for key, pat in LEAGUE_PATTERNS:
        if not re.search(pat, t):
            continue
        ex = EXCLUDE.get(key)
        if ex and re.search(ex, t + " " + cat):
            return None
        need = COUNTRY.get(key)
        if need and cat and not any(c in cat for c in need):
            return None
        return key
    return None


def _names(e: dict):
    tr = e.get("tournament") or {}
    ut = tr.get("uniqueTournament") or {}
    cat = (tr.get("category") or {}).get("name") or (ut.get("category") or {}).get("name") or ""
    return tr.get("name", "") or "", ut.get("name", "") or "", cat


def _period(desc: str):
    d = desc.lower()
    if "half" in d and "1st" not in d and "2nd" not in d and "halftime" in d.replace(" ", ""):
        return None, True
    for n, w in ((1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th")):
        if w in d:
            return n, False
    if "overtime" in d or d.strip() == "ot":
        return 5, False
    return None, False


def parse_event(e: dict, now: float = None):
    now = now or time.time()
    tn, utn, cat = _names(e)
    blob = (tn + " " + utn).lower()
    if any(w in blob for w in FAKE) or any(w in (e["homeTeam"]["name"] + e["awayTeam"]["name"]).lower() for w in FAKE):
        return None
    lg = league_of(tn, cat) or league_of(utn, cat)
    period, half = _period((e.get("status") or {}).get("description", ""))
    start = (e.get("time") or {}).get("currentPeriodStartTimestamp")
    return dict(id=str(e["id"]), league=lg, home=e["homeTeam"]["name"], away=e["awayTeam"]["name"],
                home_score=(e.get("homeScore") or {}).get("current", 0) or 0,
                away_score=(e.get("awayScore") or {}).get("current", 0) or 0,
                period=period or 0, halftime=half,
                elapsed=max(0.0, now - start) if start else 0.0)  # rough: WALL-CLOCK since period start, ignores stoppages


# CLAUDE FIX: Sofascore answers 403 from Colab/Render and its "elapsed" is wall-clock time since the period
# started (a 12-min quarter lasts ~30 real minutes), which live_model reads as game seconds. The default live
# source is now SportyBet's own live feed (true game clock). Set LIVE_SCORES_SOURCE=sofascore for the old path.
LIVE_SOURCE = os.getenv("LIVE_SCORES_SOURCE", "sportybet").lower()
FIXTURES_FALLBACK = os.getenv("FIXTURES_FALLBACK", "1") != "0"


def live_games() -> list:
    if LIVE_SOURCE != "sofascore":
        try:
            from core.bookmakers import sportybet
            return sportybet.live_games_sync()
        except Exception as e:
            log.warning("sportybet live scores failed: %s: %s", type(e).__name__, e)
            return []
    return _sofascore_live()


def _sofascore_live() -> list:
    try:
        data, _ = get_json(URL, headers={"Accept": "application/json"})
    except HttpError as e:
        log.warning("sofascore failed: %s", e)
        return []
    out = []
    for e in data.get("events", []):
        try:
            g = parse_event(e)
        except Exception:
            continue
        if g and g["league"]:
            out.append(g)
    return out


FIXTURES_URL = "https://api.sofascore.com/api/v1/sport/basketball/scheduled-events/{day}"


def parse_fixture(e: dict):
    tn, ut, cat = _names(e)
    names = (tn + " " + ut + " " + e["homeTeam"]["name"] + " " + e["awayTeam"]["name"]).lower()
    if any(w in names for w in FAKE) or (e.get("status") or {}).get("type") not in (None, "notstarted"):
        return None
    lg = league_of(tn, cat) or league_of(ut, cat)
    ts = e.get("startTimestamp")
    if not lg or not ts:
        return None
    return dict(id=str(e["id"]), league=lg, home=e["homeTeam"]["name"], away=e["awayTeam"]["name"],
                commence=datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None))


def fixtures(days: int = 2) -> list:
    """Real games still to start today and tomorrow."""
    out = []
    today = datetime.now(timezone.utc).date()
    for i in range(days):
        try:
            data, _ = get_json(FIXTURES_URL.format(day=(today + timedelta(days=i)).isoformat()))
        except HttpError as e:
            log.warning("sofascore fixtures failed: %s", e)
            continue
        for e in data.get("events", []):
            try:
                g = parse_fixture(e)
            except Exception:
                continue
            if g:
                out.append(g)
    if not out and FIXTURES_FALLBACK:
        # CLAUDE FIX: Sofascore answers 403 from Colab/Render; fall back to SportyBet's own upcoming list.
        try:
            from core.bookmakers import sportybet
            out = sportybet.fixtures_sync(days)
        except Exception as e:
            log.warning("fixtures fallback failed: %s: %s", type(e).__name__, e)
    return out


async def fetch_live_scores() -> list:
    return await asyncio.to_thread(live_games)


async def fetch_fixtures() -> list:
    return await asyncio.to_thread(fixtures)
