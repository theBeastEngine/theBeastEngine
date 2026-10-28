from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC (SQLite-friendly)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ev(p: float, odds: float) -> float:
    return p * odds - 1.0


def min_odds_to_beat(fair_p: float, min_ev: float) -> float:
    """Lowest decimal odds on a manual book that still gives min_ev."""
    if not fair_p or fair_p <= 0:
        return float("inf")
    return (1.0 + min_ev) / fair_p


def round_stake(x: float, step: int = 100) -> int:
    if x <= 0:
        return 0
    return max(step, int(round(x / step)) * step)


def floor_stake(x: float, step: int = 100) -> int:
    """Round DOWN to the step so a % cap (5% of bankroll) can never be exceeded by rounding.
    Returns 0 if x is below one step (caller should skip the bet, not bump it up)."""
    if x <= 0:
        return 0
    return int(x // step) * step
