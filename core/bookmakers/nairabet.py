# core/bookmakers/nairabet.py
"""nairabet: endpoint/parser to be built from a captured response.
Set NAIRABET_URL to the basketball odds request copied from the browser (DevTools > Network).
fetch_odds then saves the raw response to data/captures/nairabet.json and returns [] until parse() is written."""
import os
from typing import Dict, List, Optional

from core.bookmakers._base import get_json, log_shape_once, logger, save_capture

BOOK = "nairabet"
URL = os.getenv("NAIRABET_URL", "")
_warned = False


def parse(payload, league_id: Optional[str] = None) -> List[Dict]:
    return []          # TODO: write from data/captures/nairabet.json using build_rows from _base


async def fetch_odds(league_id=None, *args, **kwargs) -> List[Dict]:
    global _warned
    if not URL:
        if not _warned:
            _warned = True
            logger.info("nairabet: NAIRABET_URL not set - adapter idle")
        return []
    payload = await get_json(URL)
    if not payload:
        return []
    log_shape_once(BOOK, payload)
    save_capture(BOOK, payload)
    return parse(payload, league_id)
