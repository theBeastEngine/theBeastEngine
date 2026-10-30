"""Match Tracker: ONE Telegram message per match that grows from the morning until tip-off.

Every game of the day (every league, with or without an edge) gets a card. Checks: three spread out before one hour to
tip-off, then T-60 and T-30 (FINAL). Each check adds a block to the SAME message (the message is edited in place).
When the same market shows the edge on two checks in a row the card says PLAY NOW, lists up to 5 markets (ranked) with
numbered buttons, and a small separate message (a reply to the card) pings the phone. T-60 and FINAL repeat the PLAY NOW
status. Nothing here can create a bet without an edge: a match with no Pinnacle price is only reported.
Env: TRACKER=0 turns it off (the old per-scan pre-match alerts come back), TRACK_HOURS (default 20: games starting inside
this many hours are tracked), TRACK_EVERY_SEC (300), TRACK_MAX_CHARS (3800)."""
from __future__ import annotations

import hashlib
import html
import json
import os
import time
from datetime import datetime, timedelta, timezone

from core import bbcards
from core.pinref import _ln, PINNACLE
from services.logger import get_logger

log = get_logger("tracker")
KEY = "match_tracker"
RULE = "━" * 18


def enabled() -> bool:
    return os.environ.get("TRACKER", "1").strip() != "0"


def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def track_hours() -> float:
    return _envf("TRACK_HOURS", 24)


def max_chars() -> int:
    return int(_envf("TRACK_MAX_CHARS", 3800))


# ---------------------------------------------------------------- time helpers
def _utc_epoch(dt) -> float:
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _local(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone(timedelta(hours=_envf("ALERT_TZ_OFFSET_HOURS", 1))))


def clock_text(epoch: float) -> str:
    d = _local(epoch)
    h = d.hour % 12 or 12
    return "%d:%02d%s" % (h, d.minute, "am" if d.hour < 12 else "pm")


def tip_text(epoch: float) -> str:
    return "%s | Tip-off %s" % (_local(epoch).strftime("%a %d %b"), clock_text(epoch))


def match_key(ev) -> str:
    c = ev.get("commence")
    try:
        from utils.dates import commence_dt
        c = commence_dt(c)
    except Exception:
        c = None
    day = c.strftime("%Y%m%d") if c else "x"
    raw = "%s|%s|%s|%s" % (ev.get("league"), ev.get("home"), ev.get("away"), day)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


def confirm_max_age() -> float:
    """A T-60 / FINAL confirmation needs a Pinnacle price at most this many minutes old."""
    return _envf("PIN_CONFIRM_MAX_AGE_MIN", 15.0)


def total_range(league):
    """(low, high) plausible full-game total of a league: 0.7x-1.4x of its average. A line outside is a half-game or
    team line that landed in the full-game market."""
    try:
        from config.leagues import LEAGUES
        a = float(LEAGUES[league]["avg_total"])
        return 0.7 * a, 1.4 * a
    except Exception:
        return 0.0, 1e9


# ---------------------------------------------------------------- lines (main total / spread / moneyline of a book)
def book_lines(ev, book=None) -> dict:
    """{'total': line, 'spread': home line, 'ml': (home odds, away odds)} of one book. Main line = the pair priced
    closest to even. Pinnacle when book is PINNACLE; otherwise every non-Pinnacle book (SportyBet)."""
    def pick(o):
        return (o.get("book") == PINNACLE) if book == PINNACLE else (o.get("book") != PINNACLE)
    groups = {}
    lo, hi = total_range((ev or {}).get("league"))
    for m in ("spreads", "totals", "h2h"):
        for o in ((ev or {}).get("markets") or {}).get(m, []) or []:
            if m == "totals" and _ln(o.get("line")) is not None and not (lo <= _ln(o.get("line")) <= hi):
                continue     # a half / team line filed as the full-game total: never the main line
            if pick(o) and o.get("odds"):
                groups.setdefault((m, _ln(o.get("line"))), {})[o.get("selection")] = float(o["odds"])
    out = {"total": None, "spread": None, "ml": None}
    best = {}
    for (m, ln), sides in groups.items():
        if m == "h2h":
            if "home" in sides and "away" in sides:
                out["ml"] = (sides["home"], sides["away"])
            continue
        a, b = ("home", "away") if m == "spreads" else ("over", "under")
        if ln is None or a not in sides or b not in sides:
            continue
        gap = abs(sides[a] - sides[b])
        if m not in best or gap < best[m][0]:
            best[m] = (gap, ln)
    if "totals" in best:
        out["total"] = best["totals"][1]
    if "spreads" in best:
        out["spread"] = best["spreads"][1]
    return out


def _f(x):
    return "-" if x is None else ("%.1f" % x)


def _signed(x):
    if x is None:
        return "-"
    return "0.0" if abs(x) < 0.05 else ("%+.1f" % x)


def away_pts() -> float:
    """A fall of this much (probability, 0.02 = 2 points) in Pinnacle's fair chance of our side since the previous check
    counts as 'moved away'. Such a market does not count as agreeing with the earlier check."""
    return _envf("TRACK_AWAY_PTS", 0.02)


# ---------------------------------------------------------------- schedule
LATE = ("t60", "final", "t15", "last")          # the confirmation checkpoints: T-60, T-30, T-15, T-5
LATE_NAME = {"t60": "T-60", "final": "T-30", "t15": "T-15", "last": "T-5"}


def plan_slots(now: float, tip: float) -> list:
    """Three checks spread between now and T-65 min, then T-60 and FINAL (T-30). Slots already in the past are left out
    (a game first seen late gets what is still possible)."""
    t60, t30 = tip - 3600, tip - 1800
    slots = []
    span = (t60 - 300) - now
    if span >= 3600:
        times = [now, now + span / 3, now + 2 * span / 3]
    elif span >= 1800:
        times = [now, now + span / 2]
    else:
        times = [now]
    for i, t in enumerate(times):
        slots.append(dict(t=t, label="Check %d" % (i + 1), kind="check", done=False))
    if t60 > now + 300:
        slots.append(dict(t=t60, label="Check %d: T-60" % (len(times) + 1), kind="t60", done=False))
    if t30 > now + 300:
        slots.append(dict(t=t30, label="FINAL Check: T-30", kind="final", done=False))
    elif now >= t30 and not slots:
        slots.append(dict(t=now, label="FINAL Check", kind="final", done=False))
    if tip - 900 > now + 120 and os.environ.get("T15_CHECK", "1").strip() != "0":
        slots.append(dict(t=tip - 900, label="Check: T-15", kind="t15", done=False))
    if tip - 300 > now + 120 and os.environ.get("LAST_LOOK", "1").strip() != "0":      # SportyBet can still move a line minutes before tip-off
        slots.append(dict(t=tip - 300, label="LAST LOOK: T-5", kind="last", done=False))
    if now >= t30 and slots and slots[0]["kind"] == "check":
        slots[0]["label"], slots[0]["kind"] = "FINAL Check", "final"
    return slots


# ---------------------------------------------------------------- the tracker
class MatchTracker:
    def __init__(self, beast=None, load=None, save=None, send=None, edit=None, now=None, repo=None):
        self.beast = beast
        self._repo_obj = repo
        self._load, self._save = load, save
        self._send, self._edit = send, edit
        self._now = now or time.time
        self.state = None
        self.shadow = None            # core/shadow.Shadow (paper picks), set by the engine
        self.blocks = None            # core/blocks.Blocks (time-block cards), set by the engine
        self._near = []

    def _repo(self):
        if self._repo_obj is None:
            from storage import repository as r
            self._repo_obj = r
        return self._repo_obj

    # ---- persistence
    def _state(self) -> dict:
        if self.state is None:
            blob = None
            try:
                blob = self._load() if self._load else None
            except Exception as e:
                log.warning("tracker state unreadable: %s", e)
            try:
                self.state = json.loads(blob) if blob else {}
            except Exception:
                self.state = {}
            self.state.setdefault("matches", {})
        return self.state

    def flush(self):
        try:
            if self._save and self.state is not None:
                self._save(json.dumps(self.state))
        except Exception as e:
            log.warning("tracker state not saved: %s", e)

    # ---- Telegram plumbing
    def _tg_send(self, text, markup=None, reply_to=None, silent=False):
        if self._send:
            return self._send(text, markup, reply_to, silent)
        from core import telegram_notify as tn
        from config.settings import settings
        payload = dict(chat_id=settings.TELEGRAM_CHAT_ID, text=text[:4000], parse_mode="HTML")
        if markup:
            payload["reply_markup"] = markup
        if reply_to:
            payload["reply_to_message_id"] = reply_to
            payload["allow_sending_without_reply"] = True
        if silent:
            payload["disable_notification"] = True
        res = tn.api("sendMessage", **payload)
        return ((res or {}).get("result") or {}).get("message_id")

    def _tg_edit(self, msg_id, text, markup=None):
        if self._edit:
            return self._edit(msg_id, text, markup)
        from core import telegram_notify as tn
        from config.settings import settings
        from services.client import HttpError
        payload = dict(chat_id=settings.TELEGRAM_CHAT_ID, message_id=msg_id, text=text[:4000], parse_mode="HTML",
                       reply_markup=markup or {"inline_keyboard": []})
        try:
            tn.api("editMessageText", **payload)
            return True
        except HttpError as e:
            s = str(e)
            if "not modified" in s:
                return True
            if "not found" in s or "can't be edited" in s:
                return False
            raise

    # ---- Pinnacle / candidates (all through the engine's own, already tested pieces)
    def _pin(self, ev):
        try:
            return self.beast.pinref.match(dict(league=ev["league"], home=ev["home"], away=ev["away"],
                                                commence=ev.get("commence")))
        except Exception as e:
            log.warning("tracker pinnacle lookup failed: %s", e)
            return None

    def _edges(self, ev, held_reason):
        """(ranked, capped list of qualifying markets of this game right now). Empty when the game is on hold."""
        from core import gates
        if held_reason:
            return []
        try:
            near = []
            cands = self.beast.pinref.candidates(ev, near=near)
            self._near = near
        except Exception as e:
            log.warning("tracker candidates failed for %s: %s", ev.get("home"), e)
            return []
        cands = [c for c in cands if gates.passes_win_prob(c, "prematch") and c["ev"] <= gates.max_ev("prematch") + 1e-9]
        for c in cands:
            c["pin_real"] = True
            c["mode"] = "prematch"
        ranked = sorted(cands, key=gates.score, reverse=True)
        return gates.select_for_game(ranked, "prematch")

    @staticmethod
    def _mk(p):
        return "%s|%s|%s" % (p.get("market"), p.get("selection"), _ln(p.get("line")))

    # ---- one check of one match
    def _check(self, m, ev, slot, now):
        from core import telegram_notify as tn
        pin = self._pin(ev)
        held = None
        try:
            held = self.beast._hold_reason(dict(league=ev["league"], home=ev["home"], away=ev["away"],
                                                commence=ev.get("commence"))) if pin else None
        except Exception:
            held = None
        soft = book_lines(ev, None)
        pl = book_lines(pin, PINNACLE) if pin else None
        self._near = []
        edges = self._edges(ev, held) if pin else []
        near = list(self._near)
        keys = [self._mk(p) for p in edges]
        prev_keys = set(m["checks"][-1]["keys"]) if m["checks"] else set()
        prev_p = (m["checks"][-1].get("pmap") or {}) if m["checks"] else {}
        pmap = {self._mk(p): p.get("p") for p in edges}
        moves = {k: (v - prev_p[k]) for k, v in pmap.items() if k in prev_p and v is not None and prev_p[k] is not None}
        away = [p for p in edges if moves.get(self._mk(p), 0.0) < -away_pts()]
        # direction of Pinnacle: a market whose fair chance FELL since the previous check does not count as agreeing
        consistent = [p for p in edges if self._mk(p) in prev_keys and moves.get(self._mk(p), 0.0) >= -away_pts()]
        # markets that were PLAY NOW earlier stay in the list while they still hold (T-60 / FINAL look at them)
        playing = [mk for mk in m.get("play_keys", [])]
        holding = [p for p in edges if self._mk(p) in playing]
        status = "watch"
        if not pin:
            status = "nopin"
        elif held:
            status = "hold"
        elif consistent or (m.get("play_keys") and holding):
            status = "play"
        elif edges:
            status = "seen"
        stale = getattr(self, "_stale", None)
        if stale is not None and slot["kind"] in LATE and status == "play":
            status = "seen"          # never confirm a pick on a Pinnacle price older than confirm_max_age()
            log.warning("pick held back: Pinnacle price for %s v %s is %.0f min old", m["home"], m["away"], stale)
            try:
                if self.shadow is not None:
                    day = _local(now).strftime("%Y%m%d")
                    d = self.shadow.meta().setdefault("stale", {})
                    d[day] = d.get(day, 0) + 1
                    for k in [k for k in d if k < (_local(now) - timedelta(days=5)).strftime("%Y%m%d")]:
                        del d[k]
                    self.shadow.flush()
            except Exception:
                pass
        chk = dict(t=now, label=slot["label"], kind=slot["kind"], status=status, keys=keys, held=held,
                   soft=soft, pin=pl, tipin=m["tip"] - now,
                   stale=(None if stale is None else (stale if stale != float("inf") else 9999)),
                   inj=self._injury(ev) if pin else None, change=self._change(m, soft, pl, ev))
        picks = []
        if status == "play":
            new = [p for p in consistent if self._mk(p) not in playing]
            if new:
                self._persist_new(m, new, ev)
            picks = [dict(self._pick_view(m, p)) for p in edges if self._mk(p) in m.get("play_keys", [])]
        for q in picks:
            q["move"] = moves.get(self._mk(q))
            meta = m.get("picks", {}).get(self._mk(q))
            if meta is not None:
                meta["last"] = {k: q.get(k) for k in ("market", "selection", "line", "home", "away", "odds", "ev", "p",
                                                      "fair_odds", "book_margin", "move") if q.get(k) is not None}
        try:
            chk["why"] = self._why(edges, near)
            try:
                bad = self.beast.pinref.mismatch(ev) if pin else None
            except Exception:
                bad = None
            if bad:
                chk["why"] = ["Data check failed, no pick from this game: " + bad] + list(chk["why"] or [])
            if slot["kind"] == "final" and status in ("watch", "seen") and self.shadow is not None:
                self.shadow.record(m.get("key") or match_key(ev), ev, edges, near)
        except Exception as e:
            log.warning("tracker shadow pick failed: %s", e)
        try:
            rows = self.beast.pinref.table_rows(ev) if pin else []
            lo, hi = total_range(ev.get("league"))
            rows = [r for r in rows if r.get("market") != "totals" or (r.get("line") is not None and lo <= r["line"] <= hi)]
            if rows:
                from config.settings import settings
                chk["table"] = bbcards.compare_table(rows, m["home"], m["away"])
                chk["result"] = bbcards.plain_result(rows, m["home"], m["away"], settings.MIN_EV)
                chk["extra"] = bbcards.extra_markets(ev, pin)
        except Exception as ex:
            log.warning("tracker price table failed for %s v %s: %s", m.get("home"), m.get("away"), ex)
        chk["pmap"] = pmap
        chk["away"] = ["%s (%+.1f pts)" % ("%s: %s" % (tn._mlabel(p), tn.describe(p)), 100 * moves[self._mk(p)]) for p in away]
        chk["rows"] = picks
        chk["hint"] = ("%s: %s" % (tn._mlabel(edges[0]), tn.describe(edges[0]))) if edges else None
        chk["gone"] = [self._desc_key(m, k) for k in m.get("play_keys", []) if k not in keys]
        m["checks"].append(chk)
        return chk

    @staticmethod
    def _why(edges, near):
        """Top 3 markets of the game with their win chance and the rule each one misses (core/reasons.py)."""
        from core.reasons import why_lines
        return why_lines(edges, near, 3, "prematch")

    def _injury(self, ev):
        try:
            return self.beast.injuries.note(ev["league"], ev["home"], ev["away"])
        except Exception:
            return None

    def _change(self, m, soft, pl, ev):
        """Plain-English list of what moved since the previous check."""
        if not m["checks"]:
            return []
        last = m["checks"][-1]
        out = []
        ps, pp = last.get("pin"), pl
        if ps and pp:
            for name, key in (("total", "total"), ("spread", "spread")):
                a, b = ps.get(key), pp.get(key)
                if a is None or b is None or abs(a - b) < 1e-9:
                    continue
                if key == "total":
                    out.append("Pinnacle total %s %.1f" % ("fell" if b < a else "rose", abs(b - a)))
                else:
                    out.append("Pinnacle moved %s from %+.1f to %+.1f" % (ev["home"], a, b))
        ls = last.get("soft") or {}
        moved = []
        for key, name in (("total", "total"), ("spread", "spread")):
            a, b = ls.get(key), soft.get(key)
            if a is not None and b is not None and abs(a - b) > 1e-9:
                moved.append("SportyBet %s %.1f to %.1f" % (name, a, b))
        if out and not moved:
            out[-1] += ", SportyBet did not follow."
        elif out:
            out[-1] += "."
        out += [x + "." for x in moved]
        return out

    # ---- picks created at PLAY NOW
    def _pick_view(self, m, p):
        meta = m.get("picks", {}).get(self._mk(p), {})
        q = dict(p)
        q["id"] = meta.get("id")
        q["slot"] = meta.get("slot")
        return q

    def _desc_key(self, m, k):
        return (m.get("picks", {}).get(k) or {}).get("desc") or k

    def _persist_new(self, m, new, ev):
        from core import telegram_notify as tn
        from core.kelly import compute_stake, cap_match_stakes
        repo = self._repo()
        from utils.dates import commence_dt
        if self.beast and self.beast.breaker_tripped():
            return
        room = 5 - len(m.get("play_keys", []))
        new = new[:max(0, room)]
        if not new:
            return
        bankroll = repo.bankroll_current()
        stakes = [compute_stake(bankroll) for _ in new]
        earlier = sum((v.get("stake") or 0) for v in m.get("picks", {}).values())
        try:
            stakes = cap_match_stakes(stakes, bankroll, earlier)
        except Exception:
            pass
        m.setdefault("picks", {})
        for p, st in zip(new, stakes):
            if st <= 0:
                continue
            p = dict(p, stake=st, mode="prematch")
            pid = repo.add_pick(mode="prematch", league=p["league"], event_id=p["event_id"], home=p["home"], away=p["away"],
                                commence=commence_dt(p.get("commence")), market=p["market"], selection=p["selection"],
                                line=p.get("line"), fair_odds=p["fair_odds"], min_odds=p["min_odds"], book=p["book"],
                                book_odds=p["odds"], ev=p["ev"], stake=st)
            p["id"] = pid
            try:
                repo.mark_sent(pid)
            except Exception:
                pass
            try:
                from core import positions
                if positions.enabled():
                    self.beast.positions.register(p)
            except Exception as e:
                log.warning("tracker position register failed: %s", e)
            k = self._mk(p)
            m.setdefault("play_keys", []).append(k)
            m["picks"][k] = dict(id=pid, stake=st, desc="%s: %s" % (tn._mlabel(p), tn.describe(p)),
                                 min_odds=p["min_odds"])

    # ---- rendering
    def _row(self, m, i, p):
        from core import telegram_notify as tn
        e = html.escape
        mark = ""
        try:
            if p.get("id") is not None:
                repo = self._repo()
                if (repo.get_pick(p["id"]) or {}).get("status") == "placed":
                    mark = " ✅"
        except Exception:
            pass
        out = ["%d. <b>%s: %s</b>%s" % (i, e(tn._mlabel(p)), e(tn.describe(p)), mark),
               "   SportyBet: %.2f | fair %.2f | %+.0f%%" % (p["odds"], p["fair_odds"], p["ev"] * 100)]
        mv = p.get("move")
        if mv is not None:
            if mv > 0.002:
                out.append("   Pinnacle moving toward us \u25B2 (%+.1f pts)" % (100 * mv))
            elif mv < -0.002:
                out.append("   Pinnacle moving away \u25BC (%+.1f pts)" % (100 * mv))
            else:
                out.append("   Pinnacle steady")
        return out

    def _numbering(self, m):
        return {k: i + 1 for i, k in enumerate(m.get("play_keys", []))}

    def _lines_block(self, m, c):
        e = html.escape
        home = e(m["home"])
        L = []
        pin = c.get("pin")
        if pin is None:
            L += ["Pinnacle", "No Pinnacle price for this league (reported only, never a bet)"]
        else:
            ml = pin.get("ml")
            L += ["Pinnacle", "Total: %s | %s: %s%s" % (_f(pin.get("total")), home, _f(pin.get("spread")),
                                                         (" | Moneyline: %.2f / %.2f" % tuple(ml)) if ml else "")]
        s = c["soft"]
        ml = s.get("ml")
        L += ["SportyBet", "Total: %s | %s: %s%s" % (_f(s.get("total")), home, _f(s.get("spread")),
                                                      (" | Moneyline: %.2f / %.2f" % tuple(ml)) if ml else "")]
        return L + self._gap_lines(c)

    @staticmethod
    def _gap_lines(c):
        pin, s = c.get("pin"), c["soft"]
        if pin is None:
            return []
        gt = None if pin.get("total") is None or s.get("total") is None else s["total"] - pin["total"]
        gs = None if pin.get("spread") is None or s.get("spread") is None else s["spread"] - pin["spread"]
        return ["Gap", "Total: %s | Spread: %s" % (_signed(gt), _signed(gs))]

    def _table_block(self, m, c):
        """Match card in the tennis / football layout: the closest-to-a-bet sentence, a price table per market, Pinnacle's chances."""
        e = html.escape
        L = ["<b>%s</b>" % e(c["result"]), "<i>Price columns: SportyBet | Pinnacle fair | edge</i>"]
        L += bbcards.table_text(c["table"])
        if c.get("stale") is not None and c["stale"] < 9999:
            L.append("Pinnacle price %.0f min old" % c["stale"])
        ch = bbcards.chances(c["table"])
        if ch:
            L.append("<b>Pinnacle's chances</b>")
            L += ["\u2022 " + e(x) for x in ch]
        rd = bbcards.match_read(c["table"])
        if rd:
            L.append("<i>Read: %s</i>" % e(rd))
        if c.get("extra"):
            L.append("Also on SportyBet, not compared (no Pinnacle price to check against): %s." % e(", ".join(c["extra"])))
        return L + self._gap_lines(c)

    def _block(self, m, c, first, log_lines):
        e = html.escape
        L = [self._head(c)]
        status = c["status"]
        num = self._numbering(m)
        if c["kind"] in LATE and status == "play":
            return self._late_block(m, c, num)
        if status == "play":
            L.append("Agreed on 2 checks in a row  <b>\U0001F514 PLAY NOW</b>")
            L.append("")
            for p in c["rows"]:
                i = num.get(self._mk(p), 0)
                L += self._row(m, i, p)
            L.append("(up to 5 markets, ranked)")
            L += ["", "<b>Log</b>"] + log_lines
            for a in c.get("away") or []:
                L.append("<b>Pinnacle moved away from %s: the edge is fading.</b>" % e(a))
            L.append("<b>Recommendation: place before SportyBet adjusts.</b>")
            return L
        label = {"watch": "Status: WATCH (no edge yet)", "seen": "Status: WATCH (edge seen, waiting for a second check)",
                 "hold": "Status: ON HOLD (%s)" % e(str(c.get("held"))), "nopin": "Status: NO PINNACLE PRICE (reported only)"}[status]
        if first or c["kind"] in LATE:
            L.append(label)
            if c.get("stale") is not None and c["kind"] in LATE:
                L.append("<b>Not confirming: the Pinnacle price is %s min old.</b>" % (
                    "over 150" if c["stale"] > 150 else "%.0f" % c["stale"]))
            L.append("")
        L += self._table_block(m, c) if (status in ("watch", "seen") and c.get("table") and c.get("result")) else self._lines_block(m, c)
        if first:
            L += ["", "Injuries: %s" % (e(c["inj"]) if c.get("inj") else "none found")]
        else:
            L += ["", "<b>Update</b>"] + log_lines
            if status == "seen":
                L.append("<b>Possible edge%s. Waiting for a second check.</b>" % (
                    " on " + e(c["hint"]) if c.get("hint") else ""))
            elif status == "hold":
                L.append("<b>On hold: %s</b>" % e(str(c.get("held"))))
            elif status == "watch" and m.get("play_keys"):
                L.append("<b>The edge on your markets is gone.</b>")
        if status in ("watch", "seen") and c.get("why") and not m.get("play_keys"):
            L += ["", "<b>Why no pick</b>"] + ["\u2022 " + e(x) for x in c["why"]]
        for a in c.get("away") or []:
            L.append("<b>Pinnacle moved away from %s: the edge is fading.</b>" % e(a))
        if c.get("nxt"):
            L.append("Next check: %s" % clock_text(c["nxt"]))
        return L

    @staticmethod
    def _head(c):
        sep = " \u00B7 " if ":" in c["label"] else ": "
        return "<b>%s%s%s</b>" % (html.escape(c["label"]), sep, clock_text(c["t"]))

    def _log_checkpoint(self, m, c, slot):
        """Record play / skip per market at this checkpoint (graded later, see core/cplog.py and /qcheck)."""
        log_ = getattr(self, "cplog", None)
        if log_ is None or c.get("stale") is not None:
            return
        now_by = {self._mk(p): p for p in c["rows"]}
        for k in m.get("play_keys", []):
            meta = (m.get("picks") or {}).get(k) or {}
            if meta.get("id") is None:
                continue
            p = now_by.get(k)
            try:
                log_.record(meta["id"], slot["kind"], "play" if p else "skip", (p or {}).get("odds"))
            except Exception as e:
                log.warning("checkpoint not logged: %s", e)

    def verdict(self, m, c, slot) -> str:
        """The separate message sent at T-60, T-30, T-15 and T-5 for a match that has PLAY NOW markets: one clear verdict per
        market (PLAY / SKIP) and a bottom line."""
        e = html.escape
        name = LATE_NAME.get(slot["kind"], slot["label"])
        num = self._numbering(m)
        now_by = {self._mk(p): p for p in c["rows"]}
        stale = c.get("stale")
        L = ["\U0001F514 <b>%s VERDICT</b> | %s v %s (%s) | tip %s" % (name, e(m["home"]), e(m["away"]), e(str(m["league"]).upper()),
                                                                        clock_text(m["tip"])), ""]
        play = 0
        for k, i in sorted(num.items(), key=lambda kv: kv[1]):
            meta = m["picks"].get(k) or {}
            desc = e(meta.get("desc", k))
            mn = meta.get("min_odds")
            if k in now_by and stale is None:
                p = now_by[k]
                mv = p.get("move")
                arrow = "" if mv is None or abs(mv) <= 0.002 else (" (price rising)" if mv > 0 else " (price falling)")
                L.append("\u2705 <b>PLAY</b> %s | odds %.2f | edge %+.0f%%%s" % (desc, p["odds"], p["ev"] * 100, arrow))
                if mn:
                    L.append("    play only at %.2f or better" % mn)
                play += 1
            elif k in now_by:
                L.append("\u26A0\uFE0F <b>NOT CONFIRMED</b> %s | Pinnacle price too old to trust" % desc)
            else:
                L.append("\u274C <b>SKIP</b> %s | edge gone" % desc)
        for a in c.get("away") or []:
            L.append("\u26A0\uFE0F Pinnacle moved away from %s" % e(a))
        if c.get("change"):
            L.append("Moved: " + e(" ".join(c["change"])))
        L.append("")
        if stale is not None:
            L.append("<b>BOTTOM LINE: do not play on this check, the Pinnacle price could not be refreshed.</b>")
        elif play:
            L.append("<b>BOTTOM LINE: play %d of %d market%s.</b>" % (play, len(num), "" if len(num) == 1 else "s"))
        else:
            L.append("<b>BOTTOM LINE: skip this match, no market still holds.</b>")
        nxt = c.get("nxt")
        if nxt:
            L.append("Next check: %s" % clock_text(nxt))
        return "\n".join(L)

    def _late_block(self, m, c, num):
        e = html.escape
        L = [self._head(c)]
        now_by = {self._mk(p): p for p in c["rows"]}
        for k, i in sorted(num.items(), key=lambda kv: kv[1]):
            meta = m["picks"].get(k) or {}
            if k in now_by:
                p = now_by[k]
                mv = p.get("move")
                arrow = "" if mv is None or abs(mv) <= 0.002 else (" \u25B2" if mv > 0 else " \u25BC")
                L.append("Still holding: <b>%s</b> now %.2f | %+.0f%%%s" % (e(meta.get("desc", k)), p["odds"], p["ev"] * 100, arrow))
            else:
                L.append("%s: edge gone" % e(meta.get("desc", k)))
        for a in c.get("away") or []:
            L.append("<b>Pinnacle moved away from %s.</b>" % e(a))
        if c["kind"] in ("final", "t15", "last"):
            L.append("Pinnacle and SportyBet both stable." if not c.get("change") else "Moved: " + " ".join(c["change"]))
            mins = [m["picks"][k].get("min_odds") for k in num if k in now_by and m["picks"].get(k)]
            if mins:
                L.append("<b>Recommendation: play, or skip if it drops below %.2f.</b>" % min(mins))
            else:
                L.append("<b>Recommendation: skip, the edge is gone.</b>")
        elif c.get("change"):
            L.append("Moved: " + " ".join(c["change"]))
        return L

    def _summary(self, m) -> list:
        """Last lines of every match card: when Pinnacle priced it, the Play / Skip of each checkpoint, and the markets to take."""
        e = html.escape
        checks = m.get("checks") or []
        pin_i = next((i for i, c in enumerate(checks) if c.get("status") != "nopin"), None)
        if pin_i is None:
            pin = "No Pinnacle price so far (reported only, never a bet)" if checks else "not checked yet"
        elif pin_i == 0:
            pin = "Yes, from the start"
        else:
            pin = "Yes, from check %d (%s)" % (pin_i + 1, clock_text(checks[pin_i]["t"]))
        L = ["<b>SUMMARY</b>", "Pinnacle: %s" % pin]
        take = []
        for kind in LATE:
            nm = LATE_NAME[kind]
            c = next((c for c in checks if c.get("kind") == kind), None)
            if c is None:
                slot = next((s for s in m.get("slots", []) if s.get("kind") == kind), None)
                L.append("%s: %s" % (nm, ("pending, %s" % clock_text(slot["t"])) if slot and not m.get("skipped") else "not planned"))
                continue
            now_by = {self._mk(p): p for p in (c.get("rows") or [])}
            holds = [k for k in m.get("play_keys", []) if k in now_by]
            if c.get("stale") is not None:
                word = "Not confirmed (Pinnacle price too old)"
            elif c["status"] == "nopin":
                word = "Skip (no Pinnacle price)"
            elif c["status"] == "hold":
                word = "Skip (on hold)"
            elif holds:
                word = "<b>Play</b> (%d market%s)" % (len(holds), "" if len(holds) == 1 else "s")
                take = [(k, now_by[k]) for k in holds]
            elif m.get("play_keys"):
                word = "Skip (edge gone)"
                take = []
            else:
                word = "Skip (no edge)"
                take = []
            L.append("%s: %s" % (nm, word))
        if take:
            L.append("<b>Markets to take</b>")
            for k, p in take:
                meta = (m.get("picks") or {}).get(k) or {}
                mn = meta.get("min_odds")
                L.append("\u2022 %s @ %.2f%s" % (e(meta.get("desc", k)), p["odds"], (" (play at %.2f or better)" % mn) if mn else ""))
        return L

    def render(self, m) -> str:
        e = html.escape
        head = ["<b>MATCH TRACKER</b>", e(m["league"].upper()),
                "<b>%s</b> vs" % e(m["home"]), "<b>%s</b>" % e(m["away"]), tip_text(m["tip"])]
        blocks, log_lines, upd = [], [], []
        for i, c in enumerate(m["checks"]):
            if i == 0:
                s = c.get("soft") or {}
                pn = c.get("pin") or {}
                base = "Total %s / %s" % (_f((pn or s).get("total")), _f((pn or s).get("spread")))
                log_lines = ["%s: %s" % (clock_text(c["t"]), base)]
            else:
                txt = " ".join(c.get("change") or []) or "No change."
                log_lines = log_lines + ["%s: %s" % (clock_text(c["t"]), txt)]
            blocks.append(self._block(m, c, i == 0, list(log_lines)))
        summ = "\n" + RULE + "\n" + "\n".join(self._summary(m))
        out = self._join(head, blocks) + summ
        n = 0
        while len(out) > max_chars() and n < len(blocks) - 2:   # collapse the oldest blocks into one line each
            b = blocks[n]
            blocks[n] = [re_strip(b[0]) + " · " + {"play": "PLAY NOW", "watch": "WATCH", "seen": "WATCH", "hold": "HOLD",
                                                         "nopin": "NO PINNACLE"}[m["checks"][n]["status"]]]
            n += 1
            out = self._join(head, blocks) + summ
        return out

    @staticmethod
    def _join(head, blocks):
        parts = ["\n".join(head)]
        for b in blocks:
            parts.append(RULE + "\n" + "\n".join(b))
        return "\n".join(parts)

    def _keyboard(self, m):
        ids = [v["id"] for k, v in sorted(m.get("picks", {}).items(), key=lambda kv: m["play_keys"].index(kv[0]))
               if k in m.get("play_keys", []) and v.get("id") is not None]
        if not ids or m.get("skipped") or self._now() > m["tip"]:
            return {"inline_keyboard": []}
        from core import telegram_notify as tn
        placed = set()
        try:
            repo = self._repo()
            for i in ids:
                if (repo.get_pick(i) or {}).get("status") in ("placed", "won", "lost", "void"):
                    placed.add(i)
        except Exception:
            pass
        kb = tn.ids_keyboard(ids, placed)
        kb["inline_keyboard"].append([{"text": "Skip match", "callback_data": "tskip:%s" % m["key"]}])
        return kb

    # ---- publish
    def _publish(self, m, ping=None):
        text = self.render(m)
        kb = self._keyboard(m)
        if m.get("msg_id"):
            ok = False
            try:
                ok = self._tg_edit(m["msg_id"], text, kb)
            except Exception as e:
                log.warning("tracker edit failed (%s): %s", m["home"], e)
                return
            if not ok:
                m["msg_id"] = self._tg_send(text, kb)
        else:
            m["msg_id"] = self._tg_send(text, kb, silent=True)
        if ping:
            try:
                self._tg_send(ping, None, reply_to=m.get("msg_id"))
            except Exception as e:
                log.warning("tracker ping failed: %s", e)

    # ---- main loop
    def skip(self, key):
        st = self._state()
        m = st["matches"].get(key)
        if m:
            m["skipped"] = True
            self.flush()
            try:
                self._publish(m)
            except Exception:
                pass
        return bool(m)

    def tick(self, events=None):
        """Called every TRACK_EVERY_SEC. events: [(league, event)] (tests); None = read the leagues' SportyBet feeds."""
        st = self._state()
        now = self._now()
        if events is None:
            soon = any(not s["done"] and s["t"] <= now for mm in st["matches"].values() if not mm.get("skipped")
                       and mm["tip"] > now for s in mm["slots"])
            if not soon and now - getattr(self, "_last_disc", 0) < 1800:
                return                      # nothing due and no need to look for new games yet
            self._last_disc = now
            events = list(self._events())
        horizon = now + track_hours() * 3600
        for lg, ev in events:
            try:
                tipdt = None
                from utils.dates import commence_dt
                tipdt = commence_dt(ev.get("commence"))
                if not tipdt:
                    continue
                tip = _utc_epoch(tipdt)
                if tip <= now or tip > horizon:
                    continue
                key = match_key(ev)
                m = st["matches"].get(key)
                if m is None:
                    m = dict(key=key, league=lg, home=ev["home"], away=ev["away"], tip=tip, checks=[], play_keys=[],
                             picks={}, slots=plan_slots(now, tip), msg_id=None, skipped=False)
                    st["matches"][key] = m
                if m.get("skipped"):
                    continue
                due = [s for s in m["slots"] if not s["done"] and s["t"] <= now]
                if not due:
                    continue
                slot = due[-1]
                for s in due:
                    s["done"] = True
                self._stale = None
                try:
                    if slot["kind"] in LATE and (slot["kind"] in ("t60", "final") or m.get("play_keys")) and hasattr(self.beast.pinref, "force_fresh"):
                        age = self.beast.pinref.force_fresh(lg)
                        if age > confirm_max_age():
                            self._stale = age
                    else:
                        self.beast.pinref.ensure_fresh(lg)
                except Exception as e:
                    log.warning("tracker pinnacle refresh failed for %s: %s", lg, e)
                nxt = [s["t"] for s in m["slots"] if not s["done"]]
                chk = self._check(m, ev, slot, now)
                chk["nxt"] = nxt[0] if nxt else None
                ping = None
                if slot["kind"] in LATE and m.get("play_keys"):
                    ping = self.verdict(m, chk, slot)               # a separate, audible verdict message at each checkpoint
                    self._log_checkpoint(m, chk, slot)
                    m["pinged"] = True
                elif chk["status"] == "play" and not m.get("pinged"):
                    ping = "\U0001F514 <b>PLAY NOW</b> · %s v %s (%s)" % (html.escape(m["home"]), html.escape(m["away"]),
                                                                              html.escape(slot["label"]))
                    m["pinged"] = True
                self._publish(m, ping)
                self.flush()
            except Exception as e:
                log.warning("tracker failed for %s: %s", ev.get("home"), e)
        for k in [k for k, v in st["matches"].items() if v["tip"] < now - 12 * 3600]:
            del st["matches"][k]
        self.flush()
        if self.blocks is not None:
            try:
                self.blocks.update(st["matches"])
            except Exception as e:
                log.warning("tracker block cards failed: %s", e)

    def _events(self):
        from config.leagues import LEAGUE_KEYS
        from core.bookmakers import soft_feed
        for lg in LEAGUE_KEYS:
            try:
                for ev in soft_feed.upcoming(lg):
                    yield lg, ev
            except Exception as e:
                log.warning("tracker feed failed for %s: %s", lg, e)


def re_strip(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s)
