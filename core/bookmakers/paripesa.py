# core/bookmakers/paripesa.py
import os
from core.bookmakers import _onex

BOOK = "paripesa"
HOST = os.getenv("PARIPESA_HOST", "https://paripesa.com")   # set PARIPESA_HOST to the live NG mirror


async def fetch_odds(league_id=None, *args, **kwargs):
    return await _onex.fetch(BOOK, HOST, league_id)
