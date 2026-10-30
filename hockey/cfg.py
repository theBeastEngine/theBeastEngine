"""Ice hockey settings, all from environment variables (defaults are the safe, strict ones)."""
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
    return f("HK_MIN_EV", 0.04)


def max_ev() -> float:
    return f("HK_MAX_EV", 0.20)           # a bigger 'edge' is far more likely a wrong match / stale price than value


def min_p_two() -> float:
    return f("HK_MIN_P_TWOWAY", 0.45)      # puck line / total


def min_p_ml() -> float:
    return f("HK_MIN_P_ML", 0.40)          # moneyline win chance (hockey is close: favourites are rarely above 70%)


def max_odds() -> float:
    return f("HK_MAX_ODDS", 3.5)


def max_margin() -> float:
    return f("HK_MAX_MARGIN", 0.12)        # SportyBet's own overround on the market


def horizon_h() -> float:
    return f("HK_HORIZON_H", 24.0)


def tz_offset() -> float:
    return f("ALERT_TZ_OFFSET_HOURS", 1.0)


def stake() -> float:
    return f("HK_STAKE", 5000.0)


def bankroll_start() -> float:
    return f("HK_BANKROLL_START", 100000.0)


def agree_gap() -> float:
    return f("HK_AGREE_GAP", 0.12)         # SportyBet and Pinnacle may differ this much on the home win chance


def away_pts() -> float:
    return f("HK_AWAY_PTS", 0.02)          # Pinnacle fair chance falling by this much = moving away from the pick


def confirm_max_age_min() -> float:
    return f("HK_CONFIRM_MAX_AGE_MIN", 12.0)
