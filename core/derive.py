"""Fill missing Pinnacle gold rows at 0 credits from league priors (config/leagues.py).
Rows are flagged is_derived=True; real API rows always win and are never overwritten here.
Derived rows are PRIORS, not market prices: never treat one as a sharp fair price for a bet."""
import math
from config.leagues import LEAGUES
from config.markets import MARKETS, MATCHUP_TYPES
from config.settings import PINNACLE

TIER_PTS = 3.0                      # points of margin per tier step (TOP +3, MID 0, BOT -3)
TIER_VAL = {"TOP": 1, "MID": 0, "BOT": -1}
PERIOD_SHARE = {"full": 1.0, "h1": 0.5, "q1": 0.25, "q2": 0.25}
DERIVED_MARGIN = 0.025              # overround put on derived two-way prices
ALT_SPREAD_SHIFT = 3.0
ALT_TOTAL_SHIFT = 5.0


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _half(x: float) -> float:
    return math.floor(x) + 0.5


def _share(m) -> float:
    try:
        return PERIOD_SHARE[m.period]
    except KeyError:
        raise ValueError(f"market {m.key!r} has period {m.period!r}; add it to PERIOD_SHARE in core/derive.py")


def sd_for(sd: float, m) -> float:
    """Std-dev of the quantity a market prices (single source of truth for derive + selector)."""
    if m.kind == "total":
        return sd * 0.85 if m.key == "team_totals" else sd * 1.2 * math.sqrt(max(m.scale, 0.1))
    return sd * math.sqrt(_share(m))


def _prices(p_a: float):
    p_a = min(max(p_a, 0.02), 0.98)
    pa, pb = p_a * (1 + DERIVED_MARGIN), (1 - p_a) * (1 + DERIVED_MARGIN)
    return round(1 / pa, 3), round(1 / pb, 3)


def _row(league, mt, m, line, p_a, source):
    a, b = _prices(p_a)
    return dict(league=league, matchup_type=mt, market=m.key, source=source, line=line,
                side_a_odds=a, side_b_odds=b, is_derived=True)


def derive_rows(league: str, have: set = None, base: dict = None, source: str = PINNACLE) -> list:
    """have: {(matchup_type, market)} that already hold REAL rows (skipped).
    base: optional overrides for avg_total / home_adv / std_margin from real data."""
    if league not in LEAGUES:
        raise ValueError(f"unknown league {league!r}")
    cfg = {**LEAGUES[league], **(base or {})}
    have = have or set()
    T, adv, sd = cfg["avg_total"], cfg["home_adv"], cfg["std_margin"]
    if not (T > 0 and sd > 0):
        raise ValueError(f"{league}: avg_total and std_margin must be > 0")
    rows = []
    for mt in MATCHUP_TYPES:
        th, ta = mt.split("_")
        mu = adv + (TIER_VAL[th] - TIER_VAL[ta]) * TIER_PTS
        for m in MARKETS:
            if (mt, m.key) in have:
                continue
            share = _share(m)
            sd_p = sd * math.sqrt(share)
            mu_p = mu * share
            if m.kind == "h2h":
                rows.append(_row(league, mt, m, None, ncdf(mu_p / sd_p), source))
            elif m.kind == "spread":
                line = -_half(mu_p) if m.key != "alternate_spreads" else -_half(mu_p) - ALT_SPREAD_SHIFT
                rows.append(_row(league, mt, m, line, ncdf((mu_p + line) / sd_p), source))
            else:  # total
                sd_t = sd_for(sd, m)
                if m.key == "team_totals":
                    t_mean = (T + mu) / 2.0          # HOME team total
                    line = _half(t_mean)
                else:
                    t_mean = T * m.scale
                    line = _half(t_mean) + (ALT_TOTAL_SHIFT if m.key == "alternate_totals" else 0.0)
                rows.append(_row(league, mt, m, line, 1.0 - ncdf((line - t_mean) / sd_t), source))
    return rows
