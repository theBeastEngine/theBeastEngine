from core.ev_engine import edge  # re-export: one EV formula for the whole engine


def line_edge(market, side, pin_line, line):
    """Points of line value the soft book gives over the reference (positive = better for you).
    Spread lines are HOME lines for both sides. h2h or missing lines -> None."""
    if pin_line is None or line is None:
        return None
    if market == "totals":
        return (pin_line - line) if side == "over" else (line - pin_line) if side == "under" else None
    if market == "spreads":
        return (line - pin_line) if side == "home" else (pin_line - line) if side == "away" else None
    return None
