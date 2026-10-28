"""Settle a pick from period scores (pure logic, no I/O).

The user types the scores once per game (/score); every placed pick of that game is then settled by the same rules the
bookmakers use. Anything this module is not sure about returns None and is left for /result:
* quarter lines ending in .25 / .75 (split stakes), team totals, period moneylines that ended level.
Rules (verified against the market definitions in config/markets.py):
* full game = all periods + overtime; first half = Q1+Q2 (a 2-period league: period 1); second half = Q3+Q4 (period 2)
* Q4 / 2nd half WITHOUT 'ot' in the market key = regulation only; spreads_q4ot / spreads_h2ot (and totals if ever added)
  count overtime, matching Pinnacle's overtime-inclusive handicap
* spread lines are HOME lines: the home side covers when (home - away) + line > 0; exactly 0 is a push -> void
* totals: over wins above the line, under below it, equal = push -> void."""
import re

from core import gates

_SCORE = re.compile(r"^(\d{1,3})-(\d{1,3})$")


def parse_scores(tokens: list, n_periods: int) -> tuple:
    """['28-24','30-22','25-28','29-30','ot=5-3'] -> ([(28,24),...], (5,3) or None). Raises ValueError with a plain message."""
    periods, ot = [], None
    for t in tokens:
        t = t.strip().lower()
        if not t:
            continue
        if t.startswith("ot"):
            m = _SCORE.match(t.lstrip("ot").lstrip("=: "))
            if not m:
                raise ValueError("Overtime must look like ot=5-3.")
            ot = (int(m.group(1)), int(m.group(2)))
            continue
        m = _SCORE.match(t)
        if not m:
            raise ValueError("'%s' is not a score; write each period as home-away, e.g. 28-24." % t)
        periods.append((int(m.group(1)), int(m.group(2))))
    if len(periods) != n_periods:
        raise ValueError("This league has %d periods (%s): give %d scores, home-away each, e.g. %s%s." % (
            n_periods, "quarters" if n_periods == 4 else "halves", n_periods, " ".join(["28-24"] * n_periods),
            "  ot=5-3 if it went to overtime"))
    return periods, ot


def _add(*xs):
    return (sum(x[0] for x in xs), sum(x[1] for x in xs))


def period_score(period: str, with_ot: bool, periods: list, ot):
    """(home, away) for the market's period, or None when the period does not exist in this league."""
    n = len(periods)
    ots = [ot] if (with_ot and ot) else []
    if period == "full":
        return _add(*periods, *([ot] if ot else []))          # a full-game market always includes overtime
    if n == 4:
        table = {"q1": [periods[0]], "q2": [periods[1]], "q3": [periods[2]], "q4": [periods[3]] + ots,
                 "h1": [periods[0], periods[1]], "h2": [periods[2], periods[3]] + ots}
    elif n == 2:
        table = {"h1": [periods[0]], "h2": [periods[1]] + ots}
    else:
        return None
    parts = table.get(period)
    return _add(*parts) if parts else None


def settle(pick: dict, periods: list, ot) -> "str | None":
    """'won' | 'lost' | 'void' for the pick, or None = cannot be sure, settle by hand."""
    market = str(pick.get("market") or "")
    if market == "team_totals":
        return None
    kind, per = gates.kind_period(pick)
    with_ot = per == "full" or market.endswith("ot")
    sc = period_score(per, with_ot, periods, ot)
    if sc is None:
        return None
    home, away = sc
    sel = str(pick.get("selection") or "").lower()
    line = pick.get("line")
    if kind in ("spread", "total"):
        if line is None:
            return None
        line = float(line)
        if abs(line * 2 - round(line * 2)) > 1e-9:               # x.25 / x.75: split stake, not handled
            return None
    if kind == "h2h":
        if sel not in ("home", "away") or home == away:
            return None
        return "won" if (home > away) == (sel == "home") else "lost"
    if kind == "spread":
        if sel not in ("home", "away"):
            return None
        margin = (home - away) + line
        if margin == 0:
            return "void"
        return "won" if (margin > 0) == (sel == "home") else "lost"
    if kind == "total":
        if sel not in ("over", "under"):
            return None
        total = home + away
        if total == line:
            return "void"
        return "won" if (total > line) == (sel == "over") else "lost"
    return None
