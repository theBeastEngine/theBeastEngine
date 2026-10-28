"""Paper ('shadow') picks: what the engine would have bet if the rules were looser, graded silently so the rules can be
checked against real results.

At every match's FINAL check (T-30) that did NOT end in PLAY NOW, the best full-game market is recorded once:
* kind 'edge'  : a market that cleared the edge bar but was not played (e.g. only seen on one check)
* kind 'close' : no market cleared the bar; the one with the best edge (within -3 points of the bar) is recorded
Nothing is sent to Telegram. Grading uses core/grader.py (final scores) where a score source exists; /winrate and /shadow
show the results. Real bets are never touched. Env: SHADOW=0 turns recording off. The 8am report is core/dayreport.py."""
from __future__ import annotations

import json
import os
import time

from services.logger import get_logger

log = get_logger("shadow")
KEY = "shadow_picks"
FULL_GAME = ("h2h", "spreads", "totals")
MAX_KEEP = 3000


def enabled() -> bool:
    return os.environ.get("SHADOW", "1").strip() != "0"


def _iso(c):
    try:
        return c.isoformat() if hasattr(c, "isoformat") else (str(c) if c else None)
    except Exception:
        return None


class Shadow:
    def __init__(self, load=None, save=None, now=None):
        self._load, self._save = load, save
        self._now = now or time.time
        self.data = None

    def _d(self) -> dict:
        if self.data is None:
            try:
                blob = self._load() if self._load else None
                self.data = json.loads(blob) if blob else {}
            except Exception:
                self.data = {}
        return self.data

    def _recs(self) -> list:
        return [v for k, v in self._d().items() if not str(k).startswith("_")]

    def flush(self):
        try:
            if self._save and self.data is not None:
                keep = self.data
                recs = [k for k in keep if not str(k).startswith("_")]
                if len(recs) > MAX_KEEP:
                    for k in sorted(recs, key=lambda k: keep[k].get("t", 0))[:len(recs) - MAX_KEEP]:
                        del keep[k]
                self._save(json.dumps(keep))
        except Exception as e:
            log.warning("shadow picks not saved: %s", e)

    # ---- recording
    @staticmethod
    def choose(edges, near):
        """(kind, candidate) best full-game market, or None. edges = candidates that cleared the bar (ranked best first)."""
        lo = float(os.environ.get("SHADOW_MIN_ODDS", 1.15))      # a 1.03-1.10 favourite is not a meaningful paper pick
        full = [c for c in (edges or []) if c.get("market") in FULL_GAME]
        if full:
            return "edge", full[0]
        near = [c for c in (near or []) if c.get("market") in FULL_GAME and c.get("odds") and c.get("p")
                and c["odds"] >= lo]
        if near:
            return "close", max(near, key=lambda c: c.get("ev", -9))
        return None

    def record(self, key, ev, edges, near) -> bool:
        if not enabled():
            return False
        d = self._d()
        sid = "s:" + str(key)
        if sid in d:
            return False
        ch = self.choose(edges, near)
        if not ch:
            return False
        kind, c = ch
        d[sid] = dict(id=sid, shadow=True, kind=kind, t=int(self._now()), league=ev.get("league"), home=ev.get("home"),
                      away=ev.get("away"), commence=_iso(ev.get("commence")), market=c["market"],
                      selection=c["selection"], line=c.get("line"), odds=c["odds"], p=c.get("p"),
                      fair_odds=round(1.0 / c["p"], 3) if c.get("p") else None, ev=c.get("ev"), mode="paper",
                      status="new", res=None)
        self.flush()
        return True

    # ---- grading hooks (used by core/grader.py)
    def pending(self) -> list:
        return [dict(v, book_odds=v.get("odds"), took_odds=None) for v in self._recs() if not v.get("res")]

    def set_result(self, sid, res, score):
        r = self._d().get(str(sid))
        if r is not None:
            r["res"], r["score"], r["graded_t"] = res, score, int(self._now())
            self.flush()

    # ---- reports
    def rows(self, pred=None) -> list:
        """[(label, n, wins, win_pct, expected_pct, roi_pct)] for graded paper picks (void not counted). pred filters."""
        g = {}
        for v in self._recs():
            if pred is not None and not pred(v):
                continue
            if v.get("res") not in ("won", "lost") or not v.get("odds") or not v.get("fair_odds"):
                continue
            for label in ("Paper: edge seen, not played" if v["kind"] == "edge" else "Paper: closest, no edge",):
                x = g.setdefault(label, dict(n=0, w=0, exp=0.0, ret=0.0))
                x["n"] += 1
                x["w"] += v["res"] == "won"
                x["exp"] += 1.0 / v["fair_odds"]
                x["ret"] += (v["odds"] - 1) if v["res"] == "won" else -1.0
        out = []
        for label in ("Paper: edge seen, not played", "Paper: closest, no edge"):
            x = g.get(label)
            if x and x["n"]:
                out.append((label, x["n"], x["w"], 100.0 * x["w"] / x["n"], 100.0 * x["exp"] / x["n"], 100.0 * x["ret"] / x["n"]))
        return out

    def breakdown(self) -> dict:
        """Graded paper picks grouped by market, league, win-chance band and kind:
        {group: [(label, n, wins, win_pct, expected_pct, roi_pct)]} (void not counted, smallest groups last)."""
        def band(p):
            return "under 58%" if p < 0.58 else ("58-62%" if p < 0.62 else ("62-67%" if p < 0.67 else "67%+"))
        groups = {"market": {}, "league": {}, "win chance": {}, "kind": {}}
        for v in self._recs():
            if v.get("res") not in ("won", "lost") or not v.get("odds") or not v.get("p"):
                continue
            labels = {"market": {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}.get(v.get("market"), str(v.get("market"))),
                      "league": str(v.get("league")).upper(), "win chance": band(v["p"]),
                      "kind": "edge seen" if v.get("kind") == "edge" else "closest"}
            for g, lab in labels.items():
                x = groups[g].setdefault(lab, dict(n=0, w=0, exp=0.0, ret=0.0))
                x["n"] += 1
                x["w"] += v["res"] == "won"
                x["exp"] += v["p"]
                x["ret"] += (v["odds"] - 1) if v["res"] == "won" else -1.0
        out = {}
        for g, d in groups.items():
            out[g] = sorted([(lab, x["n"], x["w"], 100.0 * x["w"] / x["n"], 100.0 * x["exp"] / x["n"], 100.0 * x["ret"] / x["n"])
                             for lab, x in d.items()], key=lambda r: -r[1])
        return out

    def counts(self) -> dict:
        d = self._recs()
        return dict(total=len(d), graded=sum(1 for v in d if v.get("res")),
                    waiting=sum(1 for v in d if not v.get("res")))

    def recent(self, n=10) -> list:
        return sorted(self._recs(), key=lambda v: v.get("t", 0), reverse=True)[:n]

    def meta(self) -> dict:
        return self._d().setdefault("_meta", {})
