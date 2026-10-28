"""08:00 (Lagos) report of YESTERDAY: your bets, every PLAY NOW pick, the paper picks, and what is still ungraded.
Sent once a day by the engine (core/beast._morning_report, checked every tracker tick). Env: DAY_REPORT=0 turns it off, DAY_REPORT_HOUR (8)."""
from __future__ import annotations

import html
import os
from datetime import timedelta

from core import grader as G


def enabled() -> bool:
    return os.environ.get("DAY_REPORT", "1").strip() != "0"


def report_hour() -> int:
    try:
        return int(os.environ.get("DAY_REPORT_HOUR", 8))
    except ValueError:
        return 8


def _day(epoch):
    from core.tracker import _local
    return _local(epoch).strftime("%Y%m%d")


def due(now: float, last_day: str | None) -> bool:
    from core.tracker import _local
    d = _local(now)
    return enabled() and d.hour >= report_hour() and last_day != d.strftime("%Y%m%d")


def build(now: float, picks: list, grades: dict, shadow, bankroll=None, tickets=None, quarters=None, health=None) -> str:
    from core.tracker import _local
    y = _local(now) - timedelta(days=1)
    ykey = y.strftime("%Y%m%d")
    mine = []
    for p in picks:
        t = G._ts(p.get("commence"))
        if t is not None and _day(t) == ykey and p.get("mode") == "prematch":
            mine.append(p)
    placed = [p for p in mine if p.get("took_odds") or p.get("status") in ("placed", "won", "lost")]
    won = lost = 0
    profit = 0.0
    for p in placed:
        if p.get("status") == "won":
            won += 1
            profit += (p.get("stake") or 0) * ((p.get("took_odds") or p.get("book_odds") or 1) - 1)
        elif p.get("status") == "lost":
            lost += 1
            profit -= (p.get("stake") or 0)
    open_n = sum(1 for p in placed if p.get("status") == "placed")
    L = ["\U0001F4CB <b>DAILY REPORT</b> | %s" % html.escape(y.strftime("%a %d %b")), "━" * 15]
    if placed:
        L.append("<b>Your bets</b>: %d taken | won %d | lost %d | open %d | result %s₦%s" % (
            len(placed), won, lost, open_n, "+" if profit >= 0 else "-", format(int(round(abs(profit))), ",")))
    else:
        L.append("<b>Your bets</b>: none taken")
    rows = G.winrate_rows(mine, grades)
    allr = [r for r in rows if r[0] == "All picks"]
    if allr:
        _l, n, w, wp, ep, roi = allr[0]
        L.append("<b>PLAY NOW picks</b>: %d graded | won %d (%.0f%%) | expected %.0f%% | return %+.0f%%" % (n, w, wp, ep, roi))
    else:
        L.append("<b>PLAY NOW picks</b>: %d sent, none graded yet" % len(mine))
    ungraded = [p for p in mine if p.get("status") not in ("won", "lost", "void") and str((grades.get(str(p.get("id"))) or {}).get("res")) not in ("won", "lost", "void")]
    prow = shadow.rows(lambda v: (G._ts(v.get("commence")) is not None and _day(G._ts(v["commence"])) == ykey)) if shadow else []
    for lab, n, w, wp, ep, roi in prow:
        L.append("<b>%s</b>: %d graded | won %d (%.0f%%) | expected %.0f%% | return %+.0f%%" % (html.escape(lab), n, w, wp, ep, roi))
    if shadow:
        wait = [v for v in shadow.pending() if G._ts(v.get("commence")) is not None and _day(G._ts(v["commence"])) == ykey]
        if wait:
            L.append("Paper picks waiting for a result: %d" % len(wait))
    try:
        tr = tickets.ticket_rows(y.strftime("%a %d %b")) if tickets is not None else None
    except Exception:
        tr = None
    if tr:
        L.append("<b>Suggested tickets</b>: %d graded | won %d | expected %.0f%% | return %+.0f%%" % tr)
    try:
        qr = quarters.rows() if quarters is not None else []
    except Exception:
        qr = []
    if qr:
        L.append("<b>Quarter forecasts so far</b>: " + " | ".join("%s %+.1f (miss %.1f)" % (lab, b, m) for lab, n, b, m in qr)
                 + " pts (+ = game scored more than forecast)")
    try:
        stale = int(((shadow.meta().get("stale") or {}).get(ykey, 0)) if shadow else 0)
    except Exception:
        stale = 0
    if stale:
        L.append("<b>Held back for an old Pinnacle price</b>: %d check(s). A pick is never confirmed on a price over 15 minutes old." % stale)
    if health:
        L.append(html.escape(health))
    if ungraded:
        L.append("")
        L.append("<b>Not graded yet (%d)</b>: games the engine did not see finish live, or too close to call from the last score seen. Send /score or /result for them." % len(ungraded))
    if bankroll is not None:
        L += ["", "Bankroll ₦%s" % format(int(bankroll), ",")]
    return "\n".join(L)
