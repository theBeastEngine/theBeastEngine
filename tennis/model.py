"""Tennis markets, fair prices, edges and settlement. Only IDENTICAL markets are ever compared:
  'ml'    match winner: home / away (a retirement or walkover voids the pick)   ladder-less
  'ah'    game handicap, line = the HOME player's line      ladder {line: {home, away}}
  'es'    exact sets (best of 3): s2 = decided in 2 sets, s3 = goes to a third set
  'cs'    correct score in sets (best of 3): '2:0' '2:1' '1:2' '0:2' from the HOME player's side
  'sh'    set handicap (+-1.5 / +-2.5), line = the HOME player's line, settled on the sets won
  'total' total games over / under                          ladder {line: {over, under}}
An event is {'home','away','t' (epoch seconds), 'markets': {...}}. A line of one book is only compared with the SAME line of
the other, so a 21.5 total is never mixed with a 22.5 one. 'flip' turns an event around when a book lists the players in
the opposite order."""
from __future__ import annotations

from core.devig import devig
from tennis import cfg

SIDES = {"ml": ("home", "away"), "ah": ("home", "away"), "sh": ("home", "away"), "total": ("over", "under"),
         "es": ("s2", "s3"), "cs": ("2:0", "2:1", "1:2", "0:2")}
FLAT = ("ml", "es", "cs")          # markets with no line: {selection: odds}
SETS_FAMILY = ("sh", "es", "cs")   # all decided by the sets score: never more than one of them per match (they move together)


def ladder_put(ev: dict, mkt: str, line, sel: str, odds: float):
    if mkt in ("ml", "es", "cs"):
        ev["markets"].setdefault(mkt, {})[sel] = odds
    else:
        ev["markets"].setdefault(mkt, {}).setdefault(round(float(line), 2), {})[sel] = odds


def complete(ev: dict) -> dict:
    """The event's markets with every incomplete market removed (a market needs ALL its sides)."""
    out = {}
    m = ev.get("markets") if "markets" in ev else ev     # accepts an event or a bare market dict
    m = m or {}
    for k in FLAT:
        x = m.get(k) or {}
        if all(s in x for s in SIDES[k]):
            out[k] = {s: float(x[s]) for s in SIDES[k]}
    for k in ("ah", "sh", "total"):
        lad = {ln: d for ln, d in (m.get(k) or {}).items() if all(s in d for s in SIDES[k])
               and not (k == "total" and float(ln) < cfg.min_total_line())}
        if lad:
            out[k] = {ln: {s: float(d[s]) for s in SIDES[k]} for ln, d in lad.items()}
    return out


def flip(ev: dict) -> dict:
    """The same event seen from the other player's side (home <-> away, handicap lines negated)."""
    m = ev.get("markets") or {}
    out = {}
    if "ml" in m:
        out["ml"] = {("away" if s == "home" else "home"): v for s, v in m["ml"].items()}
    if "es" in m:
        out["es"] = dict(m["es"])
    if "cs" in m:
        out["cs"] = {"%s:%s" % tuple(reversed(s.split(":"))): v for s, v in m["cs"].items()}
    for fam in ("ah", "sh"):
        if fam in m:
            out[fam] = {round(-float(ln), 2): {("away" if s == "home" else "home"): v for s, v in d.items()} for ln, d in m[fam].items()}
    if "total" in m:
        out["total"] = {ln: dict(d) for ln, d in m["total"].items()}
    return dict(ev, home=ev.get("away"), away=ev.get("home"), markets=out)


def fair_probs(ev: dict) -> dict:
    """Pinnacle's margin-free chances: {('ml', None): {home, away}, ('ah', line): {...}, ('total', line): {...}}."""
    out = {}
    for k, v in complete(ev).items():
        if k in FLAT:
            ps = devig([v[s] for s in SIDES[k]])
            out[(k, None)] = dict(zip(SIDES[k], ps))
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
    out = []
    for k, v in complete(soft).items():
        items = [(None, v)] if k in FLAT else sorted(v.items())
        for ln, prices in items:
            fp = fair.get((k, ln))
            if not fp:
                continue
            mg = overround(prices)
            for sel, odds in prices.items():
                p = fp[sel]
                ev = p * odds - 1.0
                c = dict(mkt=k, line=ln, sel=sel, odds=odds, p=p, fair_odds=1.0 / p, ev=ev, margin=mg)
                c["why_not"] = why_not(c)
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
        return "%s to win" % (home if sel == "home" else away)
    if k == "total":
        return "%s %g games" % (sel.title(), ln)
    if k == "es":
        return "Match ends in %s sets" % sel[1:]
    if k == "cs":
        a, b = sel.split(":")
        return "%s wins %s sets to %s" % (home if int(a) > int(b) else away, max(a, b), min(a, b))
    who = home if sel == "home" else away
    val = ln if sel == "home" else -ln
    return "%s %+g %s" % (who, val, "sets" if k == "sh" else "games")


def agree(soft: dict, pin: dict):
    """Data check before any pick: do the two books describe the SAME match? Returns None when fine, else the reason."""
    a, b = complete(soft).get("ml"), complete(pin).get("ml")
    if not a or not b:
        return None
    pa = devig([a["home"], a["away"]])[0]
    pb = devig([b["home"], b["away"]])[0]
    if abs(pa - pb) > cfg.agree_gap():
        return "SportyBet and Pinnacle disagree on the win chance (%.0f%% vs %.0f%% for the first player): probably not the same match" % (
            100 * pa, 100 * pb)
    if (pa - 0.5) * (pb - 0.5) < 0 and abs(pa - 0.5) > 0.05 and abs(pb - 0.5) > 0.05:
        return "the two books name opposite favourites"
    return None


# ------------------------------------------------------------------ settlement
def settle(c: dict, hg: int, ag: int, home_won=None, sets=None):
    """hg / ag = total games won by each player. home_won: True / False (None = derive from games, only for tests).
    -> (status, ret): won | lost | void ; ret = profit per 1 unit staked. Half lines only (tennis has no quarter lines);
    a whole-number line that lands exactly is a push (void)."""
    k, ln, sel, odds = c["mkt"], c.get("line"), c["sel"], float(c["odds"])
    if k == "ml":
        if home_won is None:
            home_won = hg > ag
        win = (sel == "home") == bool(home_won)
        return ("won", odds - 1.0) if win else ("lost", -1.0)
    if k in ("es", "cs"):
        if not sets:
            return None
        got = ("s%d" % (sets[0] + sets[1])) if k == "es" else "%d:%d" % tuple(sets)
        return ("won", odds - 1.0) if got == sel else ("lost", -1.0)
    ln = float(ln)
    if k == "sh":
        if not sets:
            return None                      # needs the sets each player won
        diff = (sets[0] + ln - sets[1]) if sel == "home" else (sets[1] - ln - sets[0])
    elif k == "total":
        diff = (hg + ag - ln) if sel == "over" else (ln - hg - ag)
    else:
        diff = (hg + ln - ag) if sel == "home" else (ag - ln - hg)
    if diff > 1e-9:
        return "won", odds - 1.0
    if diff < -1e-9:
        return "lost", -1.0
    return "void", 0.0
