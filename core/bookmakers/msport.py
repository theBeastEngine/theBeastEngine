# core/bookmakers/msport.py
"""MSport. Reuses the SportyBet (Sportradar-style) parser. MSPORT_URL must be set from a captured
browser request (DevTools > Network); with no URL this returns [] and logs once."""
import os

from core.bookmakers import sportybet
from core.bookmakers._base import get_json, log_shape_once, logger, save_capture

BOOK = "msport"
URL = os.getenv("MSPORT_URL", "")
_warned = False


async def fetch_odds(league_id=None, *args, **kwargs):
    global _warned
    if not URL:
        if not _warned:
            _warned = True
            logger.info("msport: MSPORT_URL not set - adapter idle")
        return []
    payload = await get_json(URL, headers={"Referer": "https://www.msport.com/ng/"})
    if not payload:
        return []
    log_shape_once(BOOK, payload)
    save_capture(BOOK, payload)
    return sportybet.parse_events(payload, league_id, book=BOOK)
