"""15 markets. NCAAB has 9 real markets (REAL_NCAAB); the other 6 are n/a there.
NOTE: market keys beyond h2h/spreads/totals follow The Odds API naming as I recall it;
verify against the API docs before the first paid sweep."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    key: str
    kind: str          # h2h | spread | total
    period: str        # full | h1 | h2 | q1..q4
    scale: float       # h2h/spread: share of full-game margin; total: ratio vs full-game total
    ncaab: bool        # real market on NCAAB


MARKETS = [
    Market("h2h", "h2h", "full", 1.0, True),
    Market("spreads", "spread", "full", 1.0, True),
    Market("totals", "total", "full", 1.0, True),
    Market("h2h_h1", "h2h", "h1", 0.50, True),
    Market("spreads_h1", "spread", "h1", 0.50, True),
    Market("totals_h1", "total", "h1", 0.49, True),
    Market("alternate_spreads", "spread", "full", 1.0, True),
    Market("alternate_totals", "total", "full", 1.0, True),
    Market("team_totals", "total", "full", 0.50, True),
    Market("h2h_q1", "h2h", "q1", 0.25, False),
    Market("spreads_q1", "spread", "q1", 0.25, False),
    Market("totals_q1", "total", "q1", 0.245, False),
    Market("h2h_q2", "h2h", "q2", 0.25, False),
    Market("spreads_q2", "spread", "q2", 0.25, False),
    Market("totals_q2", "total", "q2", 0.25, False),
]

# Extra period markets that exist only on the live pre-match path (never part of the 1890-row gold table):
# SportyBet lists quarter and half handicaps/totals; Pinnacle prices them through OddsPapi.
EXTRA_MARKETS = [
    Market("spreads_q3", "spread", "q3", 0.25, False),
    Market("totals_q3", "total", "q3", 0.25, False),
    Market("totals_q4", "total", "q4", 0.25, False),
    Market("totals_h2", "total", "h2", 0.50, False),
    # Pinnacle's Q4 / 2nd-half HANDICAPS count overtime; they are only compared with a book market that says so
    Market("spreads_q4ot", "spread", "q4", 0.25, False),
    Market("spreads_h2ot", "spread", "h2", 0.50, False),
]

MARKET_KEYS = [m.key for m in MARKETS]
REAL_NCAAB = [m.key for m in MARKETS if m.ncaab]
BY_KEY = {m.key: m for m in MARKETS + EXTRA_MARKETS}

# 9 matchup tiers: home tier x away tier
TIERS = ("TOP", "MID", "BOT")
MATCHUP_TYPES = [f"{h}_{a}" for h in TIERS for a in TIERS]
