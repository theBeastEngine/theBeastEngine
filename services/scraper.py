# services/scraper.py
"""
Direct soft-book prices (0 credits). Each adapter in core/bookmakers exposes
`async fetch_odds(league_id=None)` (prematch). Adapters may also expose fetch_native /
fetch_native_live (older interface); those are used if present. Nothing here invents prices.
"""
import asyncio
import os
from typing import Dict, List

from config.settings import SOFT_BOOKS
from core.bookmakers import clean_rows, get_adapter, make_row
from core.bookmakers._base import same_team  # noqa: F401  (re-exported)
from services.logger import get_logger

log = get_logger("scraper")
TIMEOUT = float(os.getenv("SCRAPER_TIMEOUT_S", "10"))


async def _run(fn, *args) -> List[Dict]:
    try:
        return list(await asyncio.wait_for(fn(*args), timeout=TIMEOUT) or [])
    except Exception as exc:
        # CLAUDE FIX: the log used to say only "native scraper failed: TimeoutError" - no book, no reason.
        log.warning("native scraper %s failed: %s: %s", getattr(fn, "__module__", "?"), type(exc).__name__, exc)
        return []


async def scrape_live_soft(league: str, home: str, away: str) -> List[Dict]:
    """Live rows from every soft book that has a live scraper plugged in (none yet = []).
    CLAUDE FIX: rows were relabelled with THIS game's home/away without checking. An adapter that returns
    several games would have put other games' prices on this game. Rows that carry their own home/away
    must now match the requested game (same orientation) or they are dropped."""
    jobs = []
    for book in SOFT_BOOKS:
        mod = get_adapter(book)
        fn = getattr(mod, "fetch_native_live", None) if mod else None
        if fn:
            jobs.append(_run(fn, league, home, away))
    batches = await asyncio.gather(*jobs) if jobs else []
    keep, dropped = [], 0
    for r in (x for b in batches for x in clean_rows(b)):
        if r.get("home") and r.get("away") and not (same_team(r["home"], home) and same_team(r["away"], away)):
            dropped += 1
            continue
        keep.append({**r, "league": league, "home": home, "away": away})
    if dropped:
        log.info("live %s %s v %s: dropped %d rows from other games / other orientation", league, home, away, dropped)
    return keep


async def scrape_prematch_soft(league: str) -> List[Dict]:
    """Prematch rows for a league (rows keep their own home/away so they can be matched to fixtures)."""
    jobs = []
    for book in SOFT_BOOKS:
        mod = get_adapter(book)
        fn = (getattr(mod, "fetch_odds", None) or getattr(mod, "fetch_native", None)) if mod else None
        if fn:
            jobs.append(_run(fn, league))
    batches = await asyncio.gather(*jobs) if jobs else []
    out = []
    for b in batches:
        for r in b:
            if make_row(r.get("match_id"), r.get("market"), r.get("side"),
                        r.get("odds"), r.get("line"), r.get("book")):
                out.append(r)
    return out


async def scrape_fallback() -> List[Dict]:
    """Kept for compatibility with older imports."""
    return []
