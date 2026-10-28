"""Two-scan confirmation for PRE-MATCH picks (the stale / glitch-price check).

A soft-book price that is stale, suspended or a one-scan feed glitch rarely shows up on two scans in a row, and real
edges usually do. A pre-match pick is therefore sent only when the same edge (same game, market, side, line) was also
seen on an EARLIER scan inside PREMATCH_CONFIRM_SEC (default 5400 s: pre-match scans run every 30 min, so
the window must be longer than that or nothing could ever be confirmed); the first sighting is remembered, not sent.
Sim and Live keep their own rule (core.gates.confirm_persistent). A pick that carries a switch_note (the position
tracker's SWITCH) is never delayed. Two calls closer together than PREMATCH_CONFIRM_GAP_SEC (60 s) count as one scan,
so pressing /beast twice does not confirm anything by itself.

Env: PREMATCH_CONFIRM_SEC=0 turns it off. State lives on ctx (memory): a restart simply starts the confirmation again."""
from __future__ import annotations

import os
import time as _t


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def prematch_window() -> float:
    return max(0.0, _f("PREMATCH_CONFIRM_SEC", 5400.0))


def min_gap() -> float:
    return max(0.0, _f("PREMATCH_CONFIRM_GAP_SEC", 60.0))


def _key(p):
    return (p.get("league"), p.get("event_id"), p.get("market"), p.get("side") or p.get("selection"), p.get("line"))


def confirm(ranked: list, mode: str, ctx, now: float = None) -> list:
    if mode != "prematch":
        from core import gates
        return gates.confirm_persistent(ranked, mode, ctx, now)
    win = prematch_window()
    if win <= 0 or ctx is None:
        return ranked
    now = _t.time() if now is None else now
    gap = min_gap()
    pend = ctx.__dict__.setdefault("pending_pre", {})
    for k in [k for k, v in pend.items() if now - v["t"] > win]:
        del pend[k]
    out = []
    for p in ranked:
        if p.get("switch_note"):
            out.append(p)
            continue
        k = _key(p)
        rec = pend.get(k)
        if rec is None:
            pend[k] = dict(t=now, n=1)
        elif now - rec["t"] >= gap:
            out.append(dict(p, confirmed_scans=rec["n"] + 1))
            pend[k] = dict(t=now, n=rec["n"] + 1)
    return out
