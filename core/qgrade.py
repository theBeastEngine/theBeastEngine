"""Grades the quarter forecasts sent in the Q1 / Q3 messages against the real quarter scores.

The messages forecast points per quarter / half (no price is involved), so the check is: how far off was each forecast
and does it lean high or low? A forecast is only useful against a bookmaker line when its typical miss is small.
Actual quarter points come from the running totals the engine saves at every break (Q1 break, half-time, Q3 break) and
from the final score when the engine saw the game end with the clock at 0:00:
  Q1 = break1,  Q2 = HT - break1,  1H = HT,  Q3 = break3 - HT,  Q4 = final - break3,  2H = final - HT.
Env: QGRADE=0 turns recording off."""
from __future__ import annotations

import json
import os
import time

from services.logger import get_logger

log = get_logger("qgrade")
KEY = "quarter_log"
LABELS = ("Q1", "Q2", "1H", "Q3", "Q4", "2H")
MAX_KEEP = 1500


def enabled() -> bool:
    return os.environ.get("QGRADE", "1").strip() != "0"


def forecasts(it, ph, ht_total=None):
    """{label: points} the Q1 / Q3 message states for this game now. None for other phases."""
    try:
        qs = {x["label"].replace(" (rest)", ""): x["exp_pts"] for x in it["quarters"]}
        cur = it["home_score"] + it["away_score"]
        if ph == 1 and "Q1" in qs and "Q2" in qs:
            return {"Q1": cur + qs["Q1"], "Q2": qs["Q2"], "1H": cur + qs["Q1"] + qs["Q2"]}
        if ph == 3 and "Q3" in qs and "Q4" in qs:
            out = {"Q4": qs["Q4"]}
            if ht_total is not None and cur >= ht_total:
                early = cur - ht_total
                out["Q3"] = early + qs["Q3"]
                out["2H"] = early + qs["Q3"] + qs["Q4"]
            return out
    except Exception:
        return None
    return None


class QGrade:
    def __init__(self, load=None, save=None, now=None, finals=None):
        self._load, self._save = load, save
        self._now = now or time.time
        self.finals = finals
        self.data = None

    def _d(self) -> dict:
        if self.data is None:
            try:
                blob = self._load() if self._load else None
                self.data = json.loads(blob) if blob else {}
            except Exception:
                self.data = {}
        return self.data

    def flush(self):
        try:
            if self._save and self.data is not None:
                d = self.data
                if len(d) > MAX_KEEP:
                    for k in sorted(d, key=lambda k: d[k].get("t", 0))[:len(d) - MAX_KEEP]:
                        del d[k]
                self._save(json.dumps(d))
        except Exception as e:
            log.warning("quarter log not saved: %s", e)

    def record(self, gk, ph, fc) -> bool:
        if not enabled() or not fc:
            return False
        d = self._d()
        rid = "q|%s|%s|%s|%s" % (gk[0], gk[1], gk[2], ph)
        if rid in d:
            return False
        d[rid] = dict(id=rid, league=gk[0], home=gk[1], away=gk[2], ph=ph, t=int(self._now()),
                      fc={k: round(float(v), 1) for k, v in fc.items()}, b1=None, ht=None, b3=None, res={})
        self.flush()
        return True

    def observe(self, q1=None, ht=None, q3=None):
        """The engine's running break totals {(league, home, away): points}; copied into the open records of those games."""
        ch = False
        for r in self._d().values():
            gk = (r["league"], r["home"], r["away"])
            for fld, src in (("b1", q1), ("ht", ht), ("b3", q3)):
                v = (src or {}).get(gk)
                if v is not None and r.get(fld) != v:
                    r[fld] = v
                    ch = True
        if ch:
            self.flush()

    def _final(self, r):
        if self.finals is None:
            return None
        try:
            d = self.finals.lookup(r["league"], r["home"], r["away"], r["t"] - 1800)
        except Exception:
            return None
        if d and float(d.get("rem") or 0) <= 0 and int(d.get("period") or 0) >= int(d.get("n") or 4):
            return int(d["hs"]) + int(d["as_"])
        return None

    @staticmethod
    def _actual(r, label, final):
        b1, ht, b3 = r.get("b1"), r.get("ht"), r.get("b3")
        try:
            if label == "Q1":
                return b1
            if label == "Q2":
                return ht - b1 if ht is not None and b1 is not None and ht >= b1 else None
            if label == "1H":
                return ht
            if label == "Q3":
                return b3 - ht if b3 is not None and ht is not None and b3 >= ht else None
            if label == "Q4":
                return final - b3 if final is not None and b3 is not None and final >= b3 else None
            if label == "2H":
                return final - ht if final is not None and ht is not None and final >= ht else None
        except Exception:
            return None
        return None

    def grade(self) -> int:
        n = 0
        for r in self._d().values():
            open_l = [k for k in r["fc"] if k not in r["res"]]
            if not open_l:
                continue
            final = self._final(r) if any(k in ("Q4", "2H") for k in open_l) else None
            for k in open_l:
                a = self._actual(r, k, final)
                if a is not None:
                    r["res"][k] = a
                    n += 1
        if n:
            self.flush()
        return n

    def rows(self):
        """[(label, n, bias, typical miss)] bias = average (actual - forecast): positive = the game scored MORE than forecast."""
        out = []
        for lab in LABELS:
            e = [r["res"][lab] - r["fc"][lab] for r in self._d().values() if lab in r.get("res", {}) and lab in r["fc"]]
            if e:
                out.append((lab, len(e), sum(e) / len(e), sum(abs(x) for x in e) / len(e)))
        return out

    def game(self, gk):
        """[(phase, forecasts, results)] of one game, earliest first."""
        out = [(r["ph"], r["fc"], r.get("res") or {}) for r in self._d().values()
               if (r["league"], r["home"], r["away"]) == tuple(gk)]
        return sorted(out, key=lambda x: str(x[0]))

    def counts(self):
        d = list(self._d().values())
        return dict(recorded=len(d), graded=sum(1 for r in d if r["fc"] and all(k in r["res"] for k in r["fc"])))
