from statistics import NormalDist
from config.leagues import LEAGUES
from config.markets import BY_KEY
from config.settings import PINNACLE
from core.derive import sd_for
from core.devig import devig

MAIN = ("h2h", "spreads", "totals")
_N = NormalDist()


def _margins(event: dict) -> dict:
    """Overround of each (book, market, line) two-way pair, e.g. 0.045 = 4.5%."""
    g = {}
    for market, outs in event["markets"].items():
        for o in outs:
            g.setdefault((o["book"], market, o["line"]), {})[o["selection"]] = o["odds"]
    return {k: sum(1 / v for v in d.values()) - 1 for k, d in g.items() if len(d) == 2}


def iter_outcomes(event: dict):
    margins = _margins(event)
    for market, outs in event["markets"].items():
        if market not in MAIN:
            continue
        for o in outs:
            yield {**o, "market": market, "side": o["selection"],
                   "book_margin": margins.get((o["book"], market, o["line"]))}


def gold_index(league: str, source: str = PINNACLE) -> dict:
    from storage import repository as repo
    return {(r["matchup_type"], r["market"]): r for r in repo.gold_rows(source, league)}


def fair_prob(row: dict, market: str, selection: str, line, league: str):
    """Fair probability of `selection` from a gold row (devigged), moved to `line` with a normal model."""
    if not row or not row.get("side_a_odds") or not row.get("side_b_odds"):
        return None
    pa = devig([row["side_a_odds"], row["side_b_odds"]])[0]
    m = BY_KEY[market]
    if m.kind != "h2h" and line is not None and row.get("line") is not None and line != row["line"]:
        sd = sd_for(LEAGUES[league]["std_margin"], m)
        z = _N.inv_cdf(min(max(pa, 1e-4), 1 - 1e-4))
        d = line - row["line"]
        z += d / sd if m.kind == "spread" else -d / sd
        pa = _N.cdf(z)
    return pa if selection in ("home", "over") else 1.0 - pa


def candidates_prematch(event: dict, idx: dict, mt: str, allow_derived: bool = False) -> list:
    out = []
    for o in iter_outcomes(event):
        row = idx.get((mt, o["market"]))
        if not row or (row["is_derived"] and not allow_derived):
            continue
        p = fair_prob(row, o["market"], o["selection"], o["line"], event["league"])
        if p:
            out.append({**o, "event_id": event["event_id"], "league": event["league"], "home": event["home"],
                        "away": event["away"], "commence": event.get("commence"), "p": p,
                        "pin_line": row.get("line")})
    return out
