# services/sports_api.py
from typing import Dict, List

from core.scores.sofascore import fetch_fixtures, fetch_live_scores


async def get_today_games() -> List[Dict]:
    """Games in progress right now (free Sofascore feed)."""
    return await fetch_live_scores()


async def get_upcoming_games() -> List[Dict]:
    """Games still to start today and tomorrow (free Sofascore feed)."""
    return await fetch_fixtures()
