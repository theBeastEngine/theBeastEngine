"""One stance per live game.

The live scan re-prices a game every ~2 minutes from the score and the clock, so two scans can disagree about the same
game. A bettor following the alerts sees "Over", then "Under", then "team B": confusing and costly. This module makes
the live alerts consistent:

* A game has two WINDOWS: window 1 = Q1 + Q2 (up to and including half-time), window 2 = Q3 + Q4 (+ overtime).
  Two-half leagues: half 1 and half 2.
* A direction is only sent after a STABLE read: the same side came out best on STABLE_SCANS scans in a row.
* The first alert on a game sets its STANCE, per family: "team" (moneyline and spread both back a team) and "total"
  (over / under). Later alerts must agree with it. A pre-match alert already sent for the game seeds the stance.
* The opposite side is only ever sent as a labelled REVERSAL: much stronger edge, higher win probability, a longer
  stable read, at most one per window, and for a team side a real swing in the score.

Pure logic: no database, no network. State is a plain dict; the caller passes load/save functions (the key/value
table in production), so every rule is unit-tested without a database."""
import json
import os
import time

from core import gates
from services.logger import get_logger

log = get_logger("stance")

KEY = "stance"
STATE_TTL_SEC = 36 * 3600
MIN_GAP_SEC = 30.0      # two observations closer than this are the same scan (a manual /live next to the scheduler)


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def stable_scans() -> int:
    """Consecutive scans with the same best side before it is sent. The live filter already needs the edge on two scans
    (confirm_persistent), so 2 here means the same side on 3 scans in a row (about 6 minutes)."""
    return max(1, int(_env_float("STANCE_STABLE_SCANS", 2)))


def rev_stable_scans() -> int:
    return max(stable_scans(), int(_env_float("STANCE_REV_STABLE_SCANS", 4)))


def rev_min_ev() -> float:
    return _env_float("STANCE_REV_MIN_EV", 0.08)


def rev_min_p() -> float:
    return _env_float("STANCE_REV_MIN_P", 0.67)


def rev_min_swing() -> float:
    """A team reversal needs the lead to have moved this many points toward the new side since the stance was set."""
    return _env_float("STANCE_REV_MIN_SWING", 6.0)


def enabled() -> bool:
    return os.environ.get("STANCE_LOCK", "1").strip() != "0"


def fam_of(p: dict) -> str:
    kind, _ = gates.kind_period(p)
    return "total" if kind == "total" else "team"


def dir_of(p: dict):
    if fam_of(p) == "total":
        return p.get("selection")
    return p.get("side") or p.get("selection")


def window_of(league: str, period) -> str:
    """'w1' (Q1-Q2 / first half) or 'w2' (Q3-Q4, overtime / second half)."""
    try:
        from core.live_model import fmt
        n = fmt(league)[0]
    except Exception:
        n = 4
    try:
        per = int(period or 1)
    except (TypeError, ValueError):
        per = 1
    return "w1" if per <= max(1, n // 2) else "w2"


def game_key(p: dict) -> str:
    return "%s|%s" % (p.get("league"), p.get("event_id") or "%s|%s" % (p.get("home"), p.get("away")))


def _diff(p: dict):
    h, a = p.get("home_score"), p.get("away_score")
    return None if h is None or a is None else int(h) - int(a)


def pretty(fam: str, d, home: str = "", away: str = "") -> str:
    if fam == "total":
        return {"over": "Over", "under": "Under"}.get(d, str(d))
    return {"home": home or "Home", "away": away or "Away"}.get(d, str(d))


class StanceBook:
    def __init__(self, load=None, save=None, clock=None):
        self._load, self._save = load, save
        self._clock = clock or time.time
        self.state = self._read()

    # ---- persistence ----
    def _read(self) -> dict:
        try:
            raw = self._load() if self._load else None
            d = json.loads(raw) if raw else {}
        except Exception as e:
            log.warning("stance state unreadable (%s); starting empty", e)
            d = {}
        return d if isinstance(d, dict) else {}

    def flush(self):
        now = self._clock()
        for k in [k for k, g in self.state.items() if now - float(g.get("t", now)) > STATE_TTL_SEC]:
            del self.state[k]
        if self._save:
            try:
                self._save(json.dumps(self.state))
            except Exception as e:
                log.warning("stance state not saved: %s", e)

    def _game(self, gk: str) -> dict:
        g = self.state.setdefault(gk, {"locks": {}, "reads": {}, "rev": {}, "t": self._clock()})
        g["t"] = self._clock()
        return g

    # ---- stance ----
    def seed(self, gk: str, seeds: dict):
        """seeds: {'team': 'home'|'away', 'total': 'over'|'under'} from alerts already sent before the game (pre-match)."""
        g = self._game(gk)
        for fam, d in (seeds or {}).items():
            if d and fam not in g["locks"]:
                g["locks"][fam] = dict(dir=d, t=self._clock(), src="pre", diff=None, period=None)

    def note_names(self, gk: str, p: dict):
        g = self._game(gk)
        g["names"] = [p.get("home"), p.get("away")]
        g["lg"] = p.get("league")

    def board(self, max_age_sec: float = 6 * 3600) -> list:
        """[(league, home, away, stance text, set_at_epoch)] for games with a lock, newest first (for /stance and the summary)."""
        out = []
        now = self._clock()
        for gk, g in self.state.items():
            locks = g.get("locks") or {}
            if not locks or now - float(g.get("t", 0)) > max_age_sec:
                continue
            h, a = (g.get("names") or ["", ""])[:2]
            when = max(float(x.get("t", 0)) for x in locks.values())
            out.append((g.get("lg") or gk.split("|")[0], h or "", a or "", self.stance_text(gk, h or "", a or ""), when))
        return sorted(out, key=lambda t: -t[4])

    def lock_of(self, gk: str, fam: str):
        return ((self.state.get(gk) or {}).get("locks") or {}).get(fam)

    def stance_text(self, gk: str, home: str = "", away: str = "") -> str:
        """e.g. 'Lakers to win · Under' (or '' when nothing is locked)."""
        parts = []
        t = self.lock_of(gk, "team")
        if t:
            parts.append("%s %s" % (pretty("team", t["dir"], home, away), "to win" if t.get("src") != "pre" else "(pre-match side)"))
        u = self.lock_of(gk, "total")
        if u:
            parts.append(pretty("total", u["dir"]))
        return " · ".join(parts)

    # ---- reads ----
    def observe(self, gk: str, picks: list):
        """picks: this scan's qualifying picks for ONE game. Counts, per family, how many scans in a row had the same
        best direction. A scan with no qualifying pick in a family resets that family's count."""
        g = self._game(gk)
        now = self._clock()
        if now - float(g.get("obs_t", 0)) < MIN_GAP_SEC:
            return
        g["obs_t"] = now
        best = {}
        for p in picks:
            f = fam_of(p)
            if f not in best or (p.get("ev") or 0) > (best[f].get("ev") or 0):
                best[f] = p
        for f in ("team", "total"):
            p = best.get(f)
            r = g["reads"].get(f) or {}
            if p is None:
                g["reads"][f] = dict(dir=None, n=0)
            elif r.get("dir") == dir_of(p):
                g["reads"][f] = dict(dir=r["dir"], n=int(r.get("n", 0)) + 1)
            else:
                g["reads"][f] = dict(dir=dir_of(p), n=1)

    def reads(self, gk: str, fam: str) -> dict:
        return ((self.state.get(gk) or {}).get("reads") or {}).get(fam) or {"dir": None, "n": 0}

    # ---- decisions ----
    def decide(self, p: dict) -> dict:
        """-> {'action': 'send'|'hold'|'block', 'reversal': bool, 'reason': str}.
        send: allowed now (also sets/keeps the stance once sent: call commit()). hold: not stable yet. block: contradicts
        the stance and does not meet the reversal rule."""
        gk, fam, d = game_key(p), fam_of(p), dir_of(p)
        r = self.reads(gk, fam)
        lock = self.lock_of(gk, fam)
        if lock is None:
            if r.get("dir") == d and r.get("n", 0) >= stable_scans():
                return dict(action="send", reversal=False, reason="stable read x%d" % r["n"])
            return dict(action="hold", reversal=False, reason="read not stable yet (%d/%d)" % (
                r.get("n", 0) if r.get("dir") == d else 0, stable_scans()))
        if lock["dir"] == d:
            return dict(action="send", reversal=False, reason="agrees with the stance")
        # opposite of the locked stance: reversal rules
        win = window_of(p.get("league"), p.get("period"))
        g = self.state.get(gk) or {}
        if int((g.get("rev") or {}).get(win, 0)) >= 1:
            return dict(action="block", reversal=False, reason="a reversal was already used in this window")
        ev, pr = p.get("ev") or 0.0, p.get("p") or 0.0
        if ev < rev_min_ev() or pr < rev_min_p():
            return dict(action="block", reversal=False, reason="opposite of the stance: edge/probability too low for a reversal")
        if r.get("dir") != d or r.get("n", 0) < rev_stable_scans():
            return dict(action="hold", reversal=False, reason="reversal read not stable yet (%d/%d)" % (
                r.get("n", 0) if r.get("dir") == d else 0, rev_stable_scans()))
        swing_txt = ""
        if fam == "team":
            now_d, lock_diff = _diff(p), lock.get("diff")
            if lock_diff is not None and now_d is not None:
                sign = 1 if d == "home" else -1
                swing = (now_d - lock_diff) * sign
                if swing < rev_min_swing():
                    return dict(action="block", reversal=False,
                                reason="opposite of the stance: the score has not moved enough (%+.0f pts)" % swing)
                swing_txt = "lead moved %+d to %+d since the stance was set" % (lock_diff, now_d)
            elif lock.get("src") == "pre":
                swing_txt = "the pre-match side no longer holds"
        return dict(action="send", reversal=True,
                    reason=("; ".join(x for x in (swing_txt, "edge %+.1f%% at %.0f%% win probability, same side on %d scans in a row"
                                                  % (ev * 100, pr * 100, r["n"])) if x)))

    def commit(self, p: dict, decision: dict):
        """Call when a pick is actually sent."""
        gk, fam, d = game_key(p), fam_of(p), dir_of(p)
        g = self._game(gk)
        old = g["locks"].get(fam)
        if old is None or decision.get("reversal"):
            g["locks"][fam] = dict(dir=d, t=self._clock(), src="reversal" if decision.get("reversal") else "live",
                                   diff=_diff(p), period=p.get("period"))
        if decision.get("reversal"):
            win = window_of(p.get("league"), p.get("period"))
            g["rev"][win] = int(g["rev"].get(win, 0)) + 1


def gate(book: StanceBook, picks: list, seeds_for=None):
    """Apply the stance rules to one scan's live picks (any number of games).
    Returns (send, held): `send` picks may be sent (tagged with 'stance', 'reversal'); `held` must not be sent now.
    seeds_for(pick) -> {'team': dir, 'total': dir} from pre-match alerts (optional)."""
    by_game = {}
    for p in picks:
        by_game.setdefault(game_key(p), []).append(p)
    send, held = [], []
    for gk, ps in by_game.items():
        if seeds_for is not None:
            try:
                book.seed(gk, seeds_for(ps[0]))
            except Exception as e:
                log.warning("stance seeds failed for %s: %s", gk, e)
        book.note_names(gk, ps[0])
        book.observe(gk, ps)
        for p in sorted(ps, key=lambda x: x.get("ev") or 0, reverse=True):
            d = book.decide(p)
            if d["action"] == "send":
                send.append(dict(p, stance_decision=d))
            else:
                held.append(dict(p, stance_decision=d))
    return send, held


def seeds_from_picks(rows: list) -> dict:
    """Pre-match picks already sent for a game -> {'team': dir, 'total': dir}. Only FULL-game picks count (a Q1 total
    says nothing about the full-game total) and skipped picks are ignored."""
    seeds = {}
    for r in sorted(rows or [], key=lambda x: x.get("ev") or 0, reverse=True):
        if (r.get("status") or "new") == "skipped":
            continue
        kind, per = gates.kind_period(r)
        if per != "full":
            continue
        f = "total" if kind == "total" else "team"
        if f in seeds:
            continue
        sel = r.get("selection")
        if sel in ("home", "away", "over", "under"):
            seeds[f] = sel
    return seeds
