import math
from config.settings import MIN_EV, MAX_EV_SANITY
from utils.helpers import min_odds_to_beat

_EPS = 1e-9   # float tolerance so an edge of exactly 4.000% is not dropped by 0.03999999999


def edge(p: float, odds: float) -> float:
    return p * odds - 1.0


def is_valid(e: float, min_ev: float = MIN_EV) -> bool:
    """Edge must clear min_ev; anything above MAX_EV_SANITY is treated as a bad line."""
    return (min_ev - _EPS) <= e <= MAX_EV_SANITY


def _ok(c) -> bool:
    p, o = c.get("p"), c.get("odds")
    return (isinstance(p, (int, float)) and isinstance(o, (int, float))
            and math.isfinite(p) and math.isfinite(o) and 0.0 < p < 1.0 and o > 1.0)


def rank(cands: list, n: int, min_ev: float = MIN_EV) -> list:
    """cands: dicts with event_id, market, selection, p (fair prob), book, odds.
    Keeps the best book per (event, market, selection, line), drops invalid edges,
    returns top n by EV. Pass min_ev=MIN_EV_LIVE for live."""
    best = {}
    for c in cands:
        if not _ok(c):
            continue
        e = edge(c["p"], c["odds"])
        if not is_valid(e, min_ev):
            continue
        k = (c["event_id"], c["market"], c["selection"], c.get("line"))
        if k not in best or e > best[k]["ev"]:
            best[k] = {**c, "ev": e, "fair_odds": 1.0 / c["p"], "min_odds": min_odds_to_beat(c["p"], min_ev)}
    # deterministic order on ties: higher EV first, then event/market/selection
    out = sorted(best.values(), key=lambda x: (-x["ev"], str(x["event_id"]), str(x["market"]), str(x["selection"])))
    return out[:n]
