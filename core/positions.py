"""Pre-match position tracking: every pre-match alert stays 'open' until the game starts, and each market is re-checked
on its own against the latest Pinnacle price.

* A market that still has its edge: nothing is sent (it shows in the hourly summary).
* WATCH:   the edge fell below the minimum but is still positive (a quiet notice).
* EXIT:    the edge is gone (negative, or the line moved a full point against the bet).
* SWITCH:  the opposite side of the same market now qualifies as a bet of its own.
A change must be seen for HOLD_MIN minutes AND on HOLD_REFRESHES separate Pinnacle refreshes before anything is sent
(so one noisy price does not flip a pick). A move of BIG_MOVE points or more acts at once, and close to tip-off the
hold time is halved. Each state is announced once per market. Markets are independent: one changing never touches the
others. Pure logic with injected persistence, like core.stance.

Also records, for every position, Pinnacle's last fair probability before tip-off (closing-line check)."""
import json
import os
import time
from datetime import datetime

from core import gates
from services.logger import get_logger

log = get_logger("positions")

KEY = "positions"
CLV_KEY = "clv"
SIDES = {"home": "away", "away": "home", "over": "under", "under": "over"}


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def hold_min() -> float:
    return _f("POS_HOLD_MIN", 10.0)


def hold_refreshes() -> int:
    return max(1, int(_f("POS_HOLD_REFRESHES", 3)))


def big_move() -> float:
    return _f("POS_BIG_MOVE_PTS", 1.0)


def near_tip_min() -> float:
    return _f("POS_NEAR_TIP_MIN", 30.0)


def enabled() -> bool:
    return os.environ.get("POSITIONS", "1").strip() != "0"


def _dt(v):
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _utcnow():
    return datetime.utcnow()


def advantage(sel: str, kind: str, line, main_line):
    """Points by which OUR line is better than Pinnacle's current main line (negative = the line moved against us).
    spread lines are HOME lines (home favourite = negative)."""
    if line is None or main_line is None:
        return None
    d = float(line) - float(main_line)
    if kind == "spread":
        return d if sel == "home" else -d
    if kind == "total":
        return -d if sel == "over" else d
    return None


def main_line(pin_ev: dict, market: str):
    """Pinnacle's main line of a two-way market = the line whose two prices are closest to even."""
    by = {}
    for o in (pin_ev.get("markets") or {}).get(market, []):
        if o.get("book") == "pinnacle" and o.get("line") is not None:
            by.setdefault(round(float(o["line"]), 3), {})[o["selection"]] = o["odds"]
    best = None
    for ln, s in by.items():
        if len(s) == 2:
            gap = abs(list(s.values())[0] - list(s.values())[1])
            if best is None or gap < best[0]:
                best = (gap, ln)
    return best[1] if best else None


class Positions:
    def __init__(self, load=None, save=None, clock=None, clv_load=None, clv_save=None):
        self._load, self._save = load, save
        self._clv_load, self._clv_save = clv_load, clv_save
        self._clock = clock or time.time
        self.items = self._read()
        self.clv_log = self._read_clv()

    def _read_clv(self) -> list:
        try:
            raw = self._clv_load() if self._clv_load else None
            d = json.loads(raw) if raw else []
        except Exception:
            d = []
        return d if isinstance(d, list) else []

    def _read(self) -> dict:
        try:
            raw = self._load() if self._load else None
            d = json.loads(raw) if raw else {}
        except Exception as e:
            log.warning("positions unreadable (%s); starting empty", e)
            d = {}
        return d if isinstance(d, dict) else {}

    def flush(self):
        if self._save:
            try:
                self._save(json.dumps(self.items))
            except Exception as e:
                log.warning("positions not saved: %s", e)
        if self._clv_save:
            try:
                self._clv_save(json.dumps(self.clv_log[-500:]))
            except Exception as e:
                log.warning("closing-line log not saved: %s", e)

    # ---- register / query ----
    def register(self, p: dict):
        """Call when a pre-match alert is sent."""
        if p.get("mode") != "prematch" or p.get("id") is None:
            return
        kind, per = gates.kind_period(p)
        self.items[str(p["id"])] = dict(
            id=p["id"], league=p.get("league"), event_id=p.get("event_id"), home=p.get("home"), away=p.get("away"),
            commence=str(p.get("commence") or ""), market=p.get("market"), kind=kind, period=per,
            sel=p.get("selection"), line=p.get("line"), odds=p.get("odds"), p0=p.get("p"), ev0=p.get("ev"),
            state="hold", bad=None, alerted={}, last_p=p.get("p"), t=self._clock())
        self.flush()

    def active(self) -> list:
        return [v for v in self.items.values() if v.get("state") not in ("closed",)]

    def for_game(self, league, event_id) -> list:
        return [v for v in self.items.values() if v.get("league") == league and v.get("event_id") == event_id
                and v.get("state") != "closed"]

    def conflicts_new(self, p: dict):
        """A NEW pre-match pick for a market family we already hold: the same family (kind + period) of the same game is
        never sent twice. Returns the open position it clashes with, or None."""
        kind, per = gates.kind_period(p)
        for v in self.for_game(p.get("league"), p.get("event_id")):
            if v["kind"] == kind and v["period"] == per:
                return v
        return None

    # ---- the check ----
    def check(self, pinref, cands: list, now=None, get_pick=None) -> list:
        """Re-price every open position. Returns the alerts to send:
        [{'pos': position, 'status': 'watch'|'exit'|'switch', 'detail': str, 'new': candidate|None}].
        pinref.match(ev) gives the Pinnacle event, pinref.refresh_id(league) a value that changes per refresh;
        cands = this scan's qualifying pre-match candidates (finalized or not) for 'switch'."""
        from core.pinref import fair_probs
        from core.ev_engine import edge
        from config.settings import settings
        now = now or _utcnow()
        alerts = []
        for v in list(self.items.values()):
            if v.get("state") == "closed":
                continue
            c = _dt(v.get("commence"))
            if c and now > c:
                self._close(v)
                continue
            ev_stub = dict(league=v["league"], event_id=v["event_id"], home=v["home"], away=v["away"], commence=v.get("commence"))
            try:
                pin = pinref.match(ev_stub)
            except Exception as e:
                log.warning("position %s: pinnacle match failed: %s", v["id"], e)
                pin = None
            if not pin:
                continue
            rid = None
            try:
                rid = pinref.refresh_id(v["league"])
            except Exception:
                pass
            status, detail, p_now, newc = self._evaluate(v, pin, cands, fair_probs, edge, settings.MIN_EV, get_pick)
            if p_now is not None:
                v["last_p"] = p_now
            alerts += self._advance(v, status, detail, rid, newc, c, now)
        self.flush()
        return alerts

    def _evaluate(self, v, pin, cands, fair_probs, edge, min_ev, get_pick):
        sel, kind, market, line = v["sel"], v["kind"], v["market"], v.get("line")
        taken = v.get("odds")
        if get_pick is not None:
            try:
                row = get_pick(v["id"])
                if row and row.get("took_odds"):
                    taken = row["took_odds"]
                if row and row.get("status") == "skipped":
                    return "hold", "", None, None
            except Exception:
                pass
        # SWITCH: the opposite side of the same market (same kind + period) now qualifies as a bet on its own
        for c in cands or []:
            if c.get("league") == v["league"] and c.get("event_id") == v["event_id"]:
                ck, cp = gates.kind_period(c)
                if ck == kind and cp == v["period"] and c.get("selection") == SIDES.get(sel):
                    if (c.get("ev") or 0) >= min_ev:
                        return "switch", "the opposite side now has a %.1f%% edge" % ((c.get("ev") or 0) * 100), None, c
        fair = fair_probs(pin)
        f = fair.get((market, None if line is None else round(float(line), 3)))
        if f and sel in f:
            p_now = f[sel]
            e = edge(p_now, taken) if taken else None
            if e is None:
                return "hold", "", p_now, None
            if e >= min_ev:
                return "hold", "", p_now, None
            if e >= 0:
                return "watch", "edge at your price is now %+.1f%% (was %+.1f%%)" % (e * 100, (v.get("ev0") or 0) * 100), p_now, None
            return "exit", "edge at your price is now %+.1f%% (was %+.1f%%)" % (e * 100, (v.get("ev0") or 0) * 100), p_now, None
        ml = main_line(pin, market)
        adv = advantage(sel, kind, line, ml)
        if adv is None or adv >= 0:
            return "hold", "", None, None
        mv = "Pinnacle's line is now %+g (you have %+g)" % (ml, line) if kind == "spread" else \
            "Pinnacle's line is now %g (you have %g)" % (ml, line)
        if adv <= -big_move():
            return "exit", "%s: %.1f points against the bet" % (mv, -adv), None, None
        return "watch", "%s: %.1f points against the bet" % (mv, -adv), None, None

    def _advance(self, v, status, detail, rid, newc, commence, now) -> list:
        out = []
        if status == "hold":
            v["bad"] = None
            if v.get("state") == "watch":      # recovered: a later slide may warn again; exit / switch stay announced
                v["alerted"].pop("watch", None)
            if v.get("state") != "hold":
                v["state"] = "hold"
            return out
        b = v.get("bad")
        if not b or b.get("status") != status:
            b = dict(status=status, t0=self._clock(), n=0, rid=None)
        if rid is None or rid != b.get("rid"):
            b["n"] = int(b.get("n", 0)) + 1
            b["rid"] = rid
        v["bad"] = b
        mins = (self._clock() - b["t0"]) / 60.0
        near = bool(commence) and (commence - now).total_seconds() / 60.0 <= near_tip_min()
        need_n = max(2, hold_refreshes() - 1) if near else hold_refreshes()
        need_min = hold_min() / 2 if near else hold_min()
        immediate = status == "exit" and "points against" in detail   # a full-point line move acts at once
        if immediate or (b["n"] >= need_n and mins >= need_min):
            if status not in v["alerted"]:
                v["alerted"][status] = self._clock()
                v["state"] = status
                out.append(dict(pos=dict(v), status=status, detail=detail, new=newc))
        return out

    def _close(self, v):
        """Game started: keep one closing-line record (our fair probability when we alerted vs Pinnacle's last)."""
        v["state"] = "closed"
        try:
            rec = dict(id=v["id"], league=v["league"], market=v["market"], sel=v["sel"], line=v.get("line"),
                       p0=v.get("p0"), p_last=v.get("last_p"), t=self._clock())
            self.clv_log.append(rec)
            del self.clv_log[:-500]
        except Exception:
            pass

    def prune(self, max_age_h=48):
        now = self._clock()
        for k in [k for k, v in self.items.items() if v.get("state") == "closed" and now - v.get("t", now) > max_age_h * 3600]:
            del self.items[k]


def summary_lines(pos: "Positions", limit=8) -> list:
    """Hourly summary: open positions and any that are not 'hold'."""
    act = pos.active()
    if not act:
        return []
    bad = [v for v in act if v.get("state") != "hold"]
    lines = ["%d open pre-match position(s), %d needing attention" % (len(act), len(bad))]
    for v in bad[:limit]:
        lines.append("%s: %s %s %s -> %s" % (v.get("home"), v.get("market"), v.get("sel"), v.get("line"), v.get("state").upper()))
    return lines
