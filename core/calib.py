"""Calibration of settled bets: predicted win chance vs what actually happened.

Pure functions, no I/O, nothing imported from the rest of the engine.
A row is a dict:
    p          our win probability for the bet (0-1)
    odds       decimal odds we took
    won        True / False, or None for open / void bets (ignored)
    close_odds optional decimal odds at kick-off, for closing-line value
"""
from __future__ import annotations

from typing import Iterable, List, Optional

EV_EDGES = (0.04, 0.06, 0.08, 0.10, 0.15)  # bucket upper bounds
MIN_N = 30  # below this the numbers are noise


def ev_of(p: float, odds: float) -> float:
    return p * odds - 1.0


def rows_from_picks(picks, closing=None) -> List[dict]:
    """Settled picks (won/lost) -> calibration rows.

    p = 1 / fair_odds (the fair price the alert was built on); odds = the price you actually took.
    closing(pick) may return the closing FAIR probability of the same side (or None): then CLV can be shown.
    """
    out = []
    for pk in picks:
        if pk.get("status") not in ("won", "lost"):
            continue
        fo = pk.get("fair_odds")
        odds = pk.get("took_odds") or pk.get("book_odds")
        try:
            fo, odds = float(fo), float(odds)
        except (TypeError, ValueError):
            continue
        if fo <= 1.0 or odds <= 1.0:
            continue
        row = {"p": 1.0 / fo, "odds": odds, "won": pk["status"] == "won", "mode": pk.get("mode")}
        if closing is not None:
            try:
                cp = closing(pk)
            except Exception:
                cp = None
            if cp and 0.0 < cp < 1.0:
                row["close_odds"] = 1.0 / cp
        out.append(row)
    return out


def _settled(rows: Iterable[dict]) -> List[dict]:
    out = []
    for r in rows:
        try:
            if r.get("won") is None:
                continue
            p, o = float(r["p"]), float(r["odds"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0.0 < p < 1.0) or o <= 1.0:
            continue
        out.append({**r, "p": p, "odds": o})
    return out


def _label(lo: Optional[float], hi: Optional[float]) -> str:
    if lo is None:
        return f"under {hi:.0%}"
    if hi is None:
        return f"{lo:.0%} and up"
    return f"{lo:.0%}-{hi:.0%}"


def _stats(rows: List[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0, "pred": None, "actual": None, "roi": None}
    wins = sum(1 for r in rows if r["won"])
    profit = sum((r["odds"] - 1.0) if r["won"] else -1.0 for r in rows)
    return {
        "n": n,
        "pred": sum(r["p"] for r in rows) / n,
        "actual": wins / n,
        "roi": profit / n,
    }


def calibration(rows: Iterable[dict], edges=EV_EDGES) -> dict:
    """Overall + per-EV-bucket predicted vs actual win rate, ROI, Brier, CLV."""
    rs = _settled(rows)
    overall = _stats(rs)
    bounds = [None, *edges, None]
    buckets = []
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        if lo is None and hi is None:
            continue
        sel = [
            r for r in rs
            if (lo is None or ev_of(r["p"], r["odds"]) >= lo)
            and (hi is None or ev_of(r["p"], r["odds"]) < hi)
        ]
        b = _stats(sel)
        b["label"] = _label(lo, hi)
        buckets.append(b)
    brier = None
    if rs:
        brier = sum((r["p"] - (1.0 if r["won"] else 0.0)) ** 2 for r in rs) / len(rs)
    clv = [
        r["odds"] / float(r["close_odds"]) - 1.0
        for r in rs
        if r.get("close_odds") and float(r["close_odds"]) > 1.0
    ]
    return {
        "overall": overall,
        "buckets": buckets,
        "brier": brier,
        "clv_avg": (sum(clv) / len(clv)) if clv else None,
        "clv_n": len(clv),
        "enough": overall["n"] >= MIN_N,
    }


def _pct(x: Optional[float], signed: bool = False) -> str:
    if x is None:
        return "-"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def format_report(cal: dict) -> str:
    """Plain-text report for Telegram (no angle brackets)."""
    o = cal["overall"]
    if o["n"] == 0:
        return "Calibration: no settled bets yet. Log them with /took and /result."
    lines = [
        "CALIBRATION (settled bets)",
        f"Bets: {o['n']}   Predicted win: {_pct(o['pred'])}   Actual win: {_pct(o['actual'])}",
        f"ROI per bet: {_pct(o['roi'], True)}   Brier: {cal['brier']:.3f}",
    ]
    if cal["clv_avg"] is not None:
        lines.append(f"CLV vs closing odds: {_pct(cal['clv_avg'], True)} over {cal['clv_n']} bets")
    lines.append("")
    lines.append("By EV bucket (n / predicted / actual / ROI)")
    for b in cal["buckets"]:
        if b["n"]:
            lines.append(
                f"{b['label']}: {b['n']} / {_pct(b['pred'])} / {_pct(b['actual'])} / {_pct(b['roi'], True)}"
            )
    lines.append("")
    if not cal["enough"]:
        lines.append(f"Under {MIN_N} settled bets: too few to judge. Keep logging.")
    elif o["actual"] < o["pred"] - 0.05:
        lines.append("Actual wins run well below predicted: the EV numbers are too optimistic. Tighten the gates.")
    elif o["actual"] > o["pred"] + 0.05:
        lines.append("Actual wins run above predicted: the model is on the cautious side.")
    else:
        lines.append("Predicted and actual win rates are close: the estimates look honest so far.")
    return "\n".join(lines)
