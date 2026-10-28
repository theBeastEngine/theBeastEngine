"""Tennis settings, all from environment variables (defaults are the safe, strict ones)."""
from __future__ import annotations

import os


def f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def i(name: str, default: int) -> int:
    return int(f(name, default))


def on(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip() != "0"


def min_ev() -> float:
    return f("TN_MIN_EV", 0.04)


def max_ev() -> float:
    return f("TN_MAX_EV", 0.12)           # a bigger 'edge' on tennis is far more likely a wrong market / stale price than value


def min_p_two() -> float:
    return f("TN_MIN_P_TWOWAY", 0.45)      # game handicap / total games


def min_p_ml() -> float:
    return f("TN_MIN_P_ML", 0.45)          # match winner: the pick must be at least this likely to win


def max_odds() -> float:
    return f("TN_MAX_ODDS", 4.0)


def max_margin() -> float:
    return f("TN_MAX_MARGIN", 0.12)        # SportyBet's own overround on the market


def horizon_h() -> float:
    return f("TN_HORIZON_H", 24.0)


def tz_offset() -> float:
    return f("ALERT_TZ_OFFSET_HOURS", 1.0)


def stake() -> float:
    return f("TN_STAKE", 5000.0)


def bankroll_start() -> float:
    return f("TN_BANKROLL_START", 100000.0)


def agree_gap() -> float:
    return f("TN_AGREE_GAP", 0.12)         # SportyBet and Pinnacle may differ this much on the home win chance


def away_pts() -> float:
    return f("TN_AWAY_PTS", 0.02)          # Pinnacle fair chance falling by this much = moving away from the pick


def confirm_max_age_min() -> float:
    return f("TN_CONFIRM_MAX_AGE_MIN", 12.0)


def min_total_line() -> float:
    return f("TN_MIN_TOTAL", 15.5)         # a best-of-3 match has at least 12 games and averages ~22: a lower 'total' is one player's games or one set
