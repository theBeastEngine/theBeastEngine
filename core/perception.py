# core/perception.py
from typing import Dict, Iterable, List, Optional, Tuple

from config.settings import SETTINGS
from core.edge_calculator import line_edge
from core.filter import (Ctx, apply_dedup, filter_with_reasons, is_liability,
                         is_margin_trap)


def _g(s, key, default=None):
    if isinstance(s, dict):
        return s.get(key, default)
    return getattr(s, key, default)


def _floor(live: bool) -> float:
    return SETTINGS.MIN_EV_LIVE if live else SETTINGS.MIN_EV


def detect_inefficiency(signal, live: bool = False) -> bool:
    """True when the pick is a real edge: EV at or above the floor, not a liability or a margin trap."""
    ev = _g(signal, "ev", 0) or 0
    if ev < _floor(live):
        return False
    return not is_liability(signal) and not is_margin_trap(signal)


def line_inflation(signal) -> Optional[float]:
    """
    Points of line value the soft book gives over the reference (positive = soft line is better for you).
    None when the market has no line or a line is missing.
    """
    return line_edge(_g(signal, "market"), _g(signal, "side"),
                     _g(signal, "pin_line"), _g(signal, "line"))


def perception_report(picks: Iterable, live: bool = False,
                      ctx: Optional[Ctx] = None,
                      remember: bool = True) -> Tuple[List, Dict[str, int]]:
    """Run every filter. Returns (kept picks, {reason: how many were dropped}).
    remember=False: the caller (core.filter.finalize) remembers only the picks it really sends,
    so a pick cut by the top-N limit is not suppressed on the next scan."""
    # CLAUDE FIX: `ctx or Ctx()` replaced a caller's context whenever it was falsy (e.g. empty), so the
    # caller's dedupe memory was silently ignored. Only a missing ctx is replaced.
    ctx = ctx if ctx is not None else Ctx()
    floor = _floor(live)

    pool, reasons = [], {}
    for s in picks:
        if (_g(s, "ev", 0) or 0) < floor:
            reasons["below_min_ev"] = reasons.get("below_min_ev", 0) + 1
        else:
            pool.append(s)

    kept, more = filter_with_reasons(pool, ctx)
    for k, v in more.items():
        reasons[k] = reasons.get(k, 0) + v

    deduped = apply_dedup(kept, ctx)
    gone = len(kept) - len(deduped)
    if gone:
        reasons["duplicate_or_recent"] = gone

    if remember:
        ctx.remember(pool)   # next scan compares against these prices and probabilities
    return deduped, reasons


def perception_filter(picks: Iterable, live: bool = False, ctx: Optional[Ctx] = None) -> List:
    """Same signature as before. Returns the picks worth sending."""
    kept, _ = perception_report(picks, live, ctx)
    return kept
