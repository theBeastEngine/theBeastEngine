# core/bookmakers/xbet.py
import os
from core.bookmakers import _onex

BOOK = "1xbet"
HOST = os.getenv("XBET_HOST", "https://1xbet.ng")


async def fetch_odds(league_id=None, *args, **kwargs):
    return await _onex.fetch(BOOK, HOST, league_id)
