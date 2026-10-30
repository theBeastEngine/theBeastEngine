"""Ice hockey markets, fair prices, edges and settlement. Everything is the FULL GAME INCLUDING overtime / shootout (the way
Pinnacle prices NHL-style games). Only IDENTICAL markets are ever compared:
  'ml'    moneyline, home / away (incl. overtime)    ladder-less
  'ah'    puck line / handicap, line = the HOME line  ladder {line: {home, away}}   (+-1.5 is the usual one)
  'total' goals over / under                          ladder {line: {over, under}}
An event is {'home','away','t' (epoch seconds), 'markets': {...}}. A line of one book is only compared with the SAME line of
the other, so a 2.5 total is never mixed with a 2.75 one and an AH -0.5 never with AH -0.25."""
from __future__ import annotations

from core.devig import devig
from hockey import cfg

SIDES = {"ml": ("home", "away"), "ah": ("home", "away"), "total": ("over", "under")}


def ladder_put(ev: dict, mkt: str, line, sel: str, odds: float):
    if mkt == "ml":
        ev["markets"].setdefault("ml", {})[sel] = odds
    else:
        ev["markets"].setdefault(mkt, {}).setdefault(round(float(line), 2), {})[sel] = odds


def complete(ev: dict) -> dict:
    """The event's markets with every incomplete market removed (a market needs ALL its sides)."""
    out = {}
    m = ev.get("markets") if "markets" in ev else ev     # accepts an event or a bare market dict
    m = m or {}
    x = m.get("ml") or {}
    if all(s in x for s in SIDES["ml"]):
        out["ml"] = {s: float(x[s]) for s in SIDES["ml"]}
    for k in ("ah", "total"):
        lad = {ln: d for ln, d in (m.get(k) or {}).items() if all(s in d for s in SIDES[k])}
        if lad:
            out[k] = {ln: {s: float(d[s]) for s in SIDES[k]} for ln, d in lad.items()}
    return out


def fair_probs(ev: dict) -> dict:
    """Pinnacle's margin-free chances: {('ml', None): {home, away}, ('ah', line): {...}, ('total', line): {...}}."""
    out = {}
    for k, v in complete(ev).items():
        if k == "ml":
            ps = devig([v[s] for s in SIDES["ml"]])
            out[("ml", None)] = dict(zip(SIDES["ml"], ps))
        else:
            for ln, d in v.items():
                ps = devig([d[s] for s in SIDES[k]])
                out[(k, ln)] = dict(zip(SIDES[k], ps))
    return out


def overround(prices: dict) -> float:
    return sum(1.0 / o for o in prices.values()) - 1.0


def candidates(soft: dict, pin: dict) -> list:
    """Every selection that exists on both books on the identical market and line, with fair chance, edge and the reason it
    is not eligible (None when it passes every gate)."""
    fair = fair_probs(pin)
    both_ml = "ml" in complete(pin) and "ml" in complete(soft)       # the moneyline is the proof that both books show the same game
    out = []
    for k, v in complete(soft).items():
        items = [(None, v)] if k == "ml" else sorted(v.items())
        for ln, prices in items:
            fp = fair.get((k, ln))
            if not fp:
                continue
            mg = overround(prices)
            for sel, odds in prices.items():
                p = fp[sel]
                ev = p * odds - 1.0
                c = dict(mkt=k, line=ln, sel=sel, odds=odds, p=p, fair_odds=1.0 / p, ev=ev, margin=mg)
                c["why_not"] = why_not(c) or (None if both_ml else "moneyline missing on one book, cannot confirm it is the same game")
                out.append(c)
    out.sort(key=lambda c: c["ev"], reverse=True)
    return out


def why_not(c: dict):
    if c["odds"] > cfg.max_odds():
        return "odds above %.1f" % cfg.max_odds()
    if c["margin"] > cfg.max_margin():
        return "SportyBet margin %.0f%% is too high" % (100 * c["margin"])
    need = cfg.min_p_ml() if c["mkt"] == "ml" else cfg.min_p_two()
    if c["p"] < need:
        return "win chance %.0f%% below %.0f%%" % (100 * c["p"], 100 * need)
    if c["ev"] < cfg.min_ev():
        return "edge %+.1f%% below %.0f%%" % (100 * c["ev"], 100 * cfg.min_ev())
    if c["ev"] > cfg.max_ev():
        return "edge %+.0f%% looks too good to be real" % (100 * c["ev"])
    return None


def pkey(c: dict) -> str:
    return "%s|%s|%s" % (c["mkt"], "" if c["line"] is None else "%g" % c["line"], c["sel"])


def describe(c: dict, home: str, away: str) -> str:
    k, ln, sel = c["mkt"], c["line"], c["sel"]
    if k == "ml":
        return "%s to win (incl. overtime)" % (home if sel == "home" else away)
    if k == "total":
        return "%s %g goals" % (sel.title(), ln)
    team = home if sel == "home" else away
    val = ln if sel == "home" else -ln
    return "%s %s" % (team, "%+g" % val)


def agree(soft: dict, pin: dict):
    """Data check before any pick: do the two books describe the SAME game? Returns None when fine, else the reason."""
    a, b = complete(soft).get("ml"), complete(pin).get("ml")
    if not a or not b:
        return None
    pa = dict(zip(SIDES["ml"], devig([a[s] for s in SIDES["ml"]])))
    pb = dict(zip(SIDES["ml"], devig([b[s] for s in SIDES["ml"]])))
    gap = abs(pa["home"] - pb["home"])
    if gap > cfg.agree_gap():
        return "SportyBet and Pinnacle disagree on the home win chance (%.0f%% vs %.0f%%): probably not the same game" % (
            100 * pa["home"], 100 * pb["home"])
    if (pa["home"] - pa["away"]) * (pb["home"] - pb["away"]) < 0 and abs(pa["home"] - pa["away"]) > 0.10 and abs(pb["home"] - pb["away"]) > 0.10:
        return "the two books name opposite favourites"
    return None


# ------------------------------------------------------------------ settlement (final score, overtime and shootout included)
def _leg(diff: float):
    return "won" if diff > 1e-9 else ("lost" if diff < -1e-9 else "void")


def _legs(line: float):
    """A quarter line (x.25 / x.75) is two half-stakes on the neighbouring half / whole lines."""
    q = round(line * 4)
    if q % 2 == 0:
        return [(line, 1.0)]
    return [(line - 0.25, 0.5), (line + 0.25, 0.5)]


def settle(c: dict, hg: int, ag: int):
    """-> (status, ret). status won | lost | void | halfwon | halflost ; ret = profit per 1 unit staked."""
    k, ln, sel, odds = c["mkt"], c.get("line"), c["sel"], float(c["odds"])
    if k == "ml":
        if hg == ag:
            return "void", 0.0                               # a final score cannot be level in hockey: never grade a guess
        res = "home" if hg > ag else "away"
        return ("won", odds - 1.0) if res == sel else ("lost", -1.0)
    ret, kinds = 0.0, []
    for line, w in _legs(float(ln)):
        if k == "total":
            diff = (hg + ag - line) if sel == "over" else (line - hg - ag)
        else:
            diff = (hg + line - ag) if sel == "home" else (ag - line - hg)
        r = _leg(diff)
        kinds.append(r)
        ret += w * ((odds - 1.0) if r == "won" else (-1.0 if r == "lost" else 0.0))
    if all(x == "won" for x in kinds):
        return "won", ret
    if all(x == "lost" for x in kinds):
        return "lost", ret
    if all(x == "void" for x in kinds):
        return "void", 0.0
    if "won" in kinds and "void" in kinds:
        return "halfwon", ret
    if "lost" in kinds and "void" in kinds:
        return "halflost", ret
    return ("halfwon" if ret > 0 else "halflost"), ret
