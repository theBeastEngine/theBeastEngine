"""Plain-English reasons built ONLY from numbers the engine really has (Pinnacle fair chance, SportyBet price and margin,
Pinnacle's direction). No invented form or stats stories. Used by the tracker card ('Why no pick') and the block cards."""
from __future__ import annotations

from config.settings import settings, MAX_ODDS, MAX_BOOK_MARGIN
from core import gates


def _label(c: dict) -> str:
    from core import telegram_notify as tn
    try:
        return "%s: %s" % (tn._mlabel(c), tn.describe(c))
    except Exception:
        return "%s %s" % (c.get("market"), c.get("selection"))


def gate_text(c: dict, mode: str = "prematch") -> str:
    """Why this market is not a pick (the first rule it fails), or why it is waiting."""
    ev, p, odds, mg = c.get("ev"), c.get("p"), c.get("odds"), c.get("book_margin")
    if ev is not None and ev < settings.MIN_EV:
        return "edge %+.1f%%, needs +%.0f%%" % (100 * ev, 100 * settings.MIN_EV)
    if p is not None and p < gates.min_win_prob(mode) - 1e-9:
        return "win chance %.0f%%, needs %.0f%%" % (100 * p, 100 * gates.min_win_prob(mode))
    if mg is not None and mg > MAX_BOOK_MARGIN:
        return "SportyBet margin %.0f%%, over the %.0f%% limit" % (100 * mg, 100 * MAX_BOOK_MARGIN)
    if odds is not None and odds > MAX_ODDS:
        return "odds %.2f, above the %.1f limit" % (odds, MAX_ODDS)
    if ev is not None and ev > gates.max_ev(mode) + 1e-9:
        return "edge %+.0f%% looks too good to be true" % (100 * ev)
    return "passes every rule, waiting for a second check"


def pinnacle_vs_book(c: dict) -> str:
    p, odds = c.get("p"), c.get("odds")
    if not p or not odds:
        return ""
    return "Pinnacle %.0f%%, SportyBet pays like %.0f%%" % (100 * p, 100 / odds)


def why_lines(edges, near, limit: int = 3, mode: str = "prematch") -> list:
    """['Total: Over 172.5 | 61% | edge +1.9%, needs +4%. Pinnacle 61%, SportyBet pays like 59%', ...] best edge first."""
    pool, seen = [], set()
    for c in list(edges or []) + list(near or []):
        if c.get("ev") is None or c.get("p") is None:
            continue
        k = (c.get("market"), c.get("selection"), c.get("line"))
        if k in seen:
            continue
        seen.add(k)
        pool.append(c)
    pool.sort(key=lambda c: c["ev"], reverse=True)
    out = []
    for c in pool[:limit]:
        out.append("%s | %.0f%% | %s. %s" % (_label(c), 100 * c["p"], gate_text(c, mode), pinnacle_vs_book(c)))
    return out


def pick_reason(p: dict) -> str:
    """One line for a PLAY NOW pick: price gap, Pinnacle's direction, book margin."""
    parts = [pinnacle_vs_book(p)]
    mv = p.get("move")
    if mv is not None:
        parts.append("Pinnacle moving toward us" if mv > 0.002 else ("Pinnacle moving away" if mv < -0.002 else "Pinnacle steady"))
    if p.get("book_margin") is not None:
        parts.append("SportyBet margin %.0f%%" % (100 * p["book_margin"]))
    return " | ".join(x for x in parts if x)
