import math
from config.settings import settings, MAX_STAKE_PCT, STAKE_ROUND
from utils.helpers import round_stake, floor_stake


def kelly_fraction(p: float, odds: float, fraction: float = 0.25) -> float:
    """Fractional Kelly (info only; staking is flat)."""
    b = odds - 1.0
    f = (p * b - (1 - p)) / b if b > 0 else 0.0
    return max(0.0, f) * fraction


def compute_stake(bankroll: float, flat: float = None, max_pct: float = MAX_STAKE_PCT,
                  step: int = STAKE_ROUND) -> int:
    """Flat stake, capped at max_pct of CURRENT bankroll, in whole steps (N100).
    Pass the CURRENT bankroll (start + settled P/L), not BANKROLL_START.
    The cap is rounded DOWN so rounding can never push a stake over it.
    Returns 0 when the cap is below one step: skip the bet, do not bump it up.
    max_pct is an explicit argument so tests do not depend on settings."""
    if bankroll is None or not math.isfinite(bankroll) or bankroll <= 0:
        return 0
    flat = settings.FLAT_STAKE if flat is None else flat
    cap = floor_stake(bankroll * max_pct + 1e-9, step)      # epsilon: 5999.999999 -> 6000, not 5900
    want = round_stake(flat, step)
    stake = min(want, cap)
    return stake if stake >= step else 0


def match_exposure_pct() -> float:
    """Most of the CURRENT bankroll that may be at risk on ONE match across all its markets (they move together).
    MATCH_EXPOSURE_PCT in the environment, default 15%; 0 turns the cap off."""
    import os
    try:
        return max(0.0, float(os.environ.get("MATCH_EXPOSURE_PCT", 0.15)))
    except (TypeError, ValueError):
        return 0.15


def cap_match_stakes(stakes: list, bankroll: float, existing: float = 0.0, pct: float = None,
                     step: int = STAKE_ROUND) -> list:
    """Scale the stakes of ONE match's alerts so that existing + new never exceeds pct of the bankroll. Every stake is
    scaled by the same factor (the EV order is kept), rounded DOWN to a step; a stake below one step becomes 0 (drop it)."""
    pct = match_exposure_pct() if pct is None else pct
    if pct <= 0 or not stakes:
        return list(stakes)
    allowed = bankroll * pct - max(0.0, existing)
    total = float(sum(stakes))
    if allowed <= 0:
        return [0] * len(stakes)
    if total <= allowed + 1e-9:
        return list(stakes)
    f = allowed / total
    return [floor_stake(s * f + 1e-9, step) for s in stakes]
