"""Live Pinnacle check for live picks.

A live pick's fair price normally comes from the model (pre-game Pinnacle line + live score). This module compares a pick
with the CURRENT Pinnacle price of the same game when OddsPapi has one:
  confirmed  -> the pick keeps going with the real live Pinnacle probability (and is dropped if that shows no edge)
  no data    -> soft mode: the pick keeps going, labelled MODEL ONLY. strict mode: it is dropped.
Env LIVE_PIN_VERIFY = off | soft (default) | strict.
A game whose 'live' prices are identical to its pre-game prices is treated as NO live data (the feed froze at tip-off)."""
import os

from core.ev_engine import edge
from core.pinref import PINNACLE, fair_probs, _ln


def mode() -> str:
    v = os.environ.get("LIVE_PIN_VERIFY", "soft").strip().lower()
    return v if v in ("off", "soft", "strict") else "soft"


def _prices(ev) -> dict:
    out = {}
    for mk, outs in ((ev or {}).get("markets") or {}).items():
        for o in outs:
            if o.get("book") == PINNACLE and o.get("odds"):
                out[(mk, _ln(o.get("line")), o.get("selection"))] = float(o["odds"])
    return out


def frozen(live_ev, pre_ev) -> bool:
    """True when every price both events share is identical (and there are enough of them to say so)."""
    a, b = _prices(live_ev), _prices(pre_ev)
    common = [k for k in a if k in b]
    return len(common) >= 4 and all(abs(a[k] - b[k]) < 1e-9 for k in common)


def haircut() -> float:
    """MODEL ONLY picks (no live Pinnacle price to check them against) must clear the gates with this much less win
    probability. Env LIVE_MODEL_ONLY_HAIRCUT (default 0.03 = 3 points); 0 turns it off."""
    try:
        return max(0.0, min(0.15, float(os.environ.get("LIVE_MODEL_ONLY_HAIRCUT", 0.03))))
    except ValueError:
        return 0.03


def _unverified(p, md, why, min_ev=None, min_p=None, max_ev=None):
    if md == "strict":
        return "drop", p, why
    h = haircut()
    q = dict(p, pin_live=False, pin_live_note=why)
    pp = p.get("p")
    if h > 0 and pp is not None and p.get("odds"):
        p2 = pp - h
        ev2 = edge(p2, p["odds"])
        if (min_ev is not None and ev2 < min_ev - 1e-9) or (min_p is not None and p2 < min_p - 1e-9):
            return "drop", p, "model only, edge gone after the %.0f-point caution cut (%s)" % (100 * h, why)
        q.update(p_model=p.get("p_model", pp), p=p2, ev=ev2, fair_odds=round(1 / p2, 3))
    return "keep", q, why


def verify(p: dict, live_ev, pre_ev=None, md: str = None, min_ev: float = None, min_p: float = None, max_ev: float = None):
    """-> (action 'keep'|'drop', pick, reason)."""
    from config.settings import settings
    from core import gates
    md = md or mode()
    if md == "off":
        return "keep", p, "off"
    min_ev = settings.MIN_EV_LIVE if min_ev is None else min_ev
    min_p = gates.min_win_prob("live") if min_p is None else min_p
    max_ev = gates.max_ev("live") if max_ev is None else max_ev
    if live_ev is None:
        return _unverified(p, md, "no live Pinnacle data for this game", min_ev, min_p, max_ev)
    if frozen(live_ev, pre_ev):
        return _unverified(p, md, "Pinnacle price unchanged since pre-game (no live price)", min_ev, min_p, max_ev)
    f = fair_probs(live_ev).get((p.get("market"), _ln(p.get("line"))))
    sel = p.get("selection")
    if not f or sel not in f:
        return _unverified(p, md, "Pinnacle has no live price on this line", min_ev, min_p, max_ev)
    pl = f[sel]
    ev = edge(pl, p["odds"])
    if ev < min_ev - 1e-9 or ev > max_ev or pl < min_p - 1e-9:
        return "drop", p, "live Pinnacle: fair %.1f%%, edge %+.1f%%" % (100 * pl, 100 * ev)
    q = dict(p, p_model=p.get("p_model", p.get("p")), p=pl, ev=ev, fair_odds=round(1 / pl, 3), pin_live=True)
    return "keep", q, "confirmed fair %.1f%%, edge %+.1f%%" % (100 * pl, 100 * ev)


def probe_text(rows: list, notes: list) -> str:
    """Plain text for /livepin: for each game in play, does OddsPapi carry a live Pinnacle price?"""
    L = ["LIVE PINNACLE PROBE", ""]
    if not rows:
        L.append("No game in play was found in the Pinnacle feed.")
    for r in rows:
        L.append("%s: %s vs %s" % (r["league"].upper(), r["home"], r["away"]))
        L.append("  Pinnacle markets now: %d" % r["n_markets"])
        L.append("  Total line: now %s, pre-game %s" % (r.get("total_now", "-"), r.get("total_pre", "-")))
        L.append("  Spread line: now %s, pre-game %s" % (r.get("spread_now", "-"), r.get("spread_pre", "-")))
        L.append("  Verdict: " + r["verdict"])
        L.append("")
    L += notes
    return "\n".join(L).strip()
