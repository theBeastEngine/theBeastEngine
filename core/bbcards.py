"""Price table and plain-words lines for the basketball match card, in the same layout as the tennis and football cards.
Pure formatting: no I/O. Rows are {market, selection, line, odds, p, fair_odds, ev} from pinref.table_rows()."""
from __future__ import annotations

import html
import re

from core import table as _table

FAMILY = (("h2h", "MONEYLINE"), ("spreads", "SPREAD (closest line)"), ("totals", "TOTAL POINTS (closest line)"))
_ORDER = {"home": 0, "away": 1, "over": 0, "under": 1}


def _team(sel, home, away):
    return home if sel == "home" else away


def compare_table(rows: list, home: str, away: str) -> list:
    """[(heading, [(label, SportyBet odds, Pinnacle fair odds, edge)])] for the closest line of each full-game market."""
    out = []
    for fam, head in FAMILY:
        cs = [r for r in rows or [] if r.get("market") == fam]
        if not cs:
            continue
        best = max(cs, key=lambda r: r["ev"])
        same = {}
        for r in cs:
            if r.get("line") == best.get("line") and (r["selection"] not in same or r["ev"] > same[r["selection"]]["ev"]):
                same[r["selection"]] = r
        rr = []
        for sel, r in sorted(same.items(), key=lambda kv: _ORDER.get(kv[0], 2)):
            if fam == "h2h":
                lab = _team(sel, home, away)
            elif fam == "spreads":
                ln = float(r["line"])
                lab = "%s %+g" % (_team(sel, home, away), ln if sel == "home" else -ln)
            else:
                lab = "%s %g" % (sel.title(), float(r["line"]))
            rr.append((lab, r["odds"], r["fair_odds"], r["ev"]))
        out.append((head, rr))
    return out


def table_text(table: list) -> list:
    return _table.table_text(table, last=True)


def chances(table: list) -> list:
    """One plain line per market: Pinnacle's chance of each outcome (1/fair odds, rescaled to 100% within the market)."""
    out = []
    for head, rows in table or []:
        rs = [r for r in rows if r[2]]
        if len(rs) != len(rows) or len(rs) < 2:
            continue
        tot = sum(1 / r[2] for r in rs)
        labs = _table.labels(rows, True)
        out.append("%s: %s" % (head.split(" (")[0].capitalize(),
                               ", ".join("%s %.0f%%" % (lab, 100 * (1 / r[2]) / tot) for lab, r in zip(labs, rs))))
    return out


def plain_result(rows: list, home: str, away: str, min_ev: float) -> str:
    """One plain sentence: how far the closest full-game market is from a bet ('' when there is nothing to compare)."""
    rows = [r for r in rows or [] if r.get("market") in ("h2h", "spreads", "totals")]
    if not rows:
        return ""
    c = max(rows, key=lambda r: r["ev"])
    what = "%s (SportyBet %.2f, Pinnacle fair %.2f)" % (_describe(c, home, away), c["odds"], c["fair_odds"])
    if c["ev"] < min_ev:
        return "NO BET. Closest to a bet: %s. Edge %+.1f%%, that is %.1f points short of the %.0f%% needed." % (
            what, 100 * c["ev"], 100 * (min_ev - c["ev"]), 100 * min_ev)
    return "NO BET YET. Closest to a bet: %s. Edge %+.1f%%, but the engine's other checks have not cleared it." % (what, 100 * c["ev"])


def _describe(c: dict, home: str, away: str) -> str:
    team = _team(c["selection"], home, away)
    if c["market"] == "h2h":
        return "%s to win" % team
    if c["market"] == "spreads":
        ln = float(c["line"])
        return "%s %+g" % (team, ln if c["selection"] == "home" else -ln)
    return "%s %g points" % (c["selection"].title(), float(c["line"]))


def match_read(table: list) -> str:
    """One plain sentence from Pinnacle's fair prices: the favourite and its chance, the expected margin and the total."""
    t = {h.split(" (")[0]: rows for h, rows in (table or [])}
    ml = t.get("MONEYLINE") or []
    out = []
    if len(ml) == 2 and all(r[2] for r in ml):
        ps = [1 / r[2] for r in ml]
        ps = [v / sum(ps) for v in ps]
        labs = _table.labels(ml, True)
        fav = 0 if ps[0] >= ps[1] else 1
        out.append("%s is the favourite, about %.0f%% to win" % (labs[fav], 100 * ps[fav]) if ps[fav] >= 0.55
                   else "an even game: %s %.0f%%, %s %.0f%%" % (labs[0], 100 * ps[0], labs[1], 100 * ps[1]))
    sp = t.get("SPREAD") or []
    m = re.search(r"([+-]\d+(?:\.\d+)?)$", sp[0][0]) if sp else None
    if m and abs(float(m.group(1))) >= 0.5:
        out.append("expected winning margin about %g points" % abs(float(m.group(1))))
    tp = t.get("TOTAL POINTS") or []
    m = re.search(r"(\d+(?:\.\d+)?)$", tp[0][0]) if tp else None
    if m:
        out.append("Pinnacle's total is %s points" % m.group(1))
    return ("; ".join(out) + ". (Pinnacle's view, not a tip.)") if out else ""


_KIND = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}
_PERIOD = {"q1": "Q1", "q2": "Q2", "q3": "Q3", "q4": "Q4", "h1": "1st half", "h2": "2nd half", "q4ot": "Q4 incl. OT", "h2ot": "2nd half incl. OT"}


def extra_markets(ev: dict, pin: dict) -> list:
    """Quarter / half markets SportyBet offers for this game that Pinnacle has no price for (named, never compared)."""
    have = {m for m, outs in ((pin or {}).get("markets") or {}).items() if any(o.get("book") == "pinnacle" for o in outs or [])}
    out = []
    for m, outs in ((ev or {}).get("markets") or {}).items():
        kind, _, per = str(m).partition("_")
        if not per or m in have or kind not in _KIND or per not in _PERIOD or not outs:
            continue
        name = "%s (%s)" % (_KIND[kind], _PERIOD[per])
        if name not in out:
            out.append(name)
    return out[:8]
