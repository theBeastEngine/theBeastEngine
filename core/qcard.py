"""One QUARTERS card per live game, edited at each break (Q1 view, then Q3 view, then forecast vs actual).

A 'TAKE' is shown only where SportyBet has a real quarter / half total line for this game right now AND the forecast is
clearly on one side of it (QTAKE_Q points for a quarter, QTAKE_H for a half). The forecast is still being graded
(see /qcheck), so every take is labelled unproven until QTAKE_PROVEN forecasts have been graded."""
from __future__ import annotations

import html
import os

LABEL_MARKET = {"Q1": "totals_q1", "Q2": "totals_q2", "Q3": "totals_q3", "Q4": "totals_q4", "1H": "totals_h1", "2H": "totals_h2"}
NAMES = {"Q1": "Q1", "Q2": "Q2", "Q3": "Q3", "Q4": "Q4", "1H": "1st half", "2H": "2nd half"}


def _f(name, d):
    try:
        return float(os.environ.get(name, d))
    except ValueError:
        return float(d)


def proven_n() -> int:
    return int(_f("QTAKE_PROVEN", 30))


def _pair(rows):
    """[{selection, line, odds}] -> (line, over_odds, under_odds) of the most balanced line, or None."""
    by = {}
    for r in rows or []:
        sel = str(r.get("selection", "")).lower()
        if sel not in ("over", "under") or r.get("line") is None or not r.get("odds"):
            continue
        by.setdefault(float(r["line"]), {})[sel] = float(r["odds"])
    best = None
    for ln, d in by.items():
        if "over" in d and "under" in d:
            gap = abs(d["over"] - d["under"])
            if best is None or gap < best[0]:
                best = (gap, ln, d["over"], d["under"])
    return best[1:] if best else None


def takes(fc: dict, qlines: dict) -> list:
    """[{label, side, line, odds, fc}] where the forecast is clearly over / under the SportyBet line."""
    out = []
    for lab, pts in (fc or {}).items():
        mk = LABEL_MARKET.get(lab)
        pr = _pair((qlines or {}).get(mk)) if mk else None
        if not pr:
            continue
        ln, o, u = pr
        thr = _f("QTAKE_H", 4.5) if lab in ("1H", "2H") else _f("QTAKE_Q", 3.0)
        d = float(pts) - ln
        if d >= thr:
            out.append(dict(label=lab, side="OVER", line=ln, odds=o, fc=float(pts)))
        elif d <= -thr:
            out.append(dict(label=lab, side="UNDER", line=ln, odds=u, fc=float(pts)))
    return out


def take_lines(fc: dict, qlines: dict) -> list:
    e = html.escape
    tk = takes(fc, qlines)
    have = [NAMES[l] for l in (fc or {}) if (qlines or {}).get(LABEL_MARKET.get(l))]
    out = []
    for t in tk:
        out.append("\U0001F449 <b>TAKE %s %g | %s</b> @ %.2f (forecast %d pts)" % (t["side"], t["line"], e(NAMES[t["label"]]), t["odds"], round(t["fc"])))
    if not tk:
        if have:
            out.append("No take: SportyBet has lines for %s but the forecast is too close to them." % ", ".join(have))
        else:
            out.append("No take: SportyBet shows no quarter / half total line for this game right now.")
    return out


def render(lg, home, away, data: dict, recs: list, graded: int) -> str:
    """data: {sec: {'1': text, '3': text}, take: {'1': [lines], '3': [lines]}}; recs: [(ph, fc, res)] of this game."""
    e = html.escape
    L = ["\U0001F4C8 <b>QUARTERS</b> | %s | %s vs %s" % (e(str(lg).upper()), e(str(home)), e(str(away))), "━" * 15]
    for ph in sorted((data.get("sec") or {})):
        sec = (data.get("sec") or {}).get(ph)
        if not sec:
            continue
        L += ["", sec]
        L += [""] + list((data.get("take") or {}).get(ph) or [])
    rows = []
    for ph, fc, res in recs:
        for lab, f in fc.items():
            if lab in res:
                rows.append("%s: forecast %d | actual %d | %s" % (NAMES.get(lab, lab), round(f), res[lab],
                                                                  "%+d" % round(res[lab] - f)))
    if rows:
        L += ["", "<b>RESULT CHECK</b>"] + [e(r) for r in rows]
    n = proven_n()
    L += ["", "<i>Forecasts are still being graded (%d of %d so far, see /qcheck). Treat takes as unproven: small stake or none.</i>" % (graded, n)
          if graded < n else "<i>%d forecasts graded so far, see /qcheck for how accurate they are.</i>" % graded]
    return "\n".join(L)
