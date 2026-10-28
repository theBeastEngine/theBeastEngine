"""Hourly scan summary: what the engine looked at, how much had a real Pinnacle price, what was closest to
qualifying (NOT bets), money status and health warnings. Collecting is cheap; formatting is pure (tested)."""
import html
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from config.settings import settings
from core import gates
from services.logger import get_logger

log = get_logger("digest")
NEAR_FLOOR = -0.03     # an edge this far below zero is still worth showing as "closest"
KEEP_NEAR = 6


def every_min() -> float:
    try:
        return float(os.environ.get("DIGEST_EVERY_MIN", 60))
    except ValueError:
        return 60.0


def _tz() -> float:
    try:
        return float(os.environ.get("ALERT_TZ_OFFSET_HOURS", "1"))
    except ValueError:
        return 1.0


def _wat(dt) -> str:
    off = _tz()
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=off))).strftime("%d %b %H:%M") + (" WAT" if off == 1 else " UTC%+g" % off)


class ScanStats:
    """Thread-safe counters for the current digest window (prematch/sim run on one thread, live on another)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.pre = None     # latest prematch scan
        self.sim = None     # latest sim scan
        self.window_start = time.time()
        self._reset_window()

    def _reset_window(self):
        self.n_pre = self.n_sim = self.n_live = 0
        self.live_games_max = self.live_priced_max = self.live_cands_max = 0
        self.live_sent = 0
        self.near = {}

    def add_near(self, items):
        for it in items or []:
            k = (it.get("league"), it.get("event_id"), it.get("market"), it.get("selection"), it.get("line"))
            old = self.near.get(k)
            if old is None or it["ev"] > old["ev"]:
                self.near[k] = it
        if len(self.near) > 60:
            keep = sorted(self.near.values(), key=lambda x: x["ev"], reverse=True)[:30]
            self.near = {(i.get("league"), i.get("event_id"), i.get("market"), i.get("selection"), i.get("line")): i
                         for i in keep}

    def record_prematch(self, n_ev, n_pin, per_lg, n_cands, n_sent, near, upcoming, games=None):
        with self._lock:
            self.n_pre += 1
            self.pre = dict(at=time.time(), games=n_ev, priced=n_pin, per_lg=dict(per_lg), cands=n_cands, sent=n_sent,
                            upcoming=upcoming, game_list=list(games or []))
            self.add_near(near)

    def record_sim(self, n_ev, n_pin, n_cands, n_sent):
        with self._lock:
            self.n_sim += 1
            self.sim = dict(at=time.time(), games=n_ev, priced=n_pin, cands=n_cands, sent=n_sent)

    def record_live(self, games, priced, cands, sent):
        with self._lock:
            self.n_live += 1
            self.live_games_max = max(self.live_games_max, games)
            self.live_priced_max = max(self.live_priced_max, priced)
            self.live_cands_max = max(self.live_cands_max, cands)
            self.live_sent += sent

    def snapshot_and_reset(self, reset: bool = True) -> dict:
        with self._lock:
            snap = dict(pre=self.pre, sim=self.sim, n_pre=self.n_pre, n_sim=self.n_sim, n_live=self.n_live,
                        live_games_max=self.live_games_max, live_priced_max=self.live_priced_max,
                        live_cands_max=self.live_cands_max, live_sent=self.live_sent,
                        near=sorted(self.near.values(), key=lambda x: x["ev"], reverse=True),
                        window_min=(time.time() - self.window_start) / 60)
            if reset:
                self.window_start = time.time()
                self._reset_window()
            return snap


def near_miss(c: dict) -> dict:
    """Compact record of a candidate that did not (or might not) qualify."""
    return dict(league=c["league"], event_id=c["event_id"], home=c["home"], away=c["away"], market=c["market"],
                selection=c["selection"], line=c.get("line"), ev=c["ev"], p=c["p"], odds=c["odds"])


def _bet_text(it) -> str:
    from core.telegram_notify import describe, _mlabel
    try:
        return "%s %s" % (_mlabel(it), describe(it))
    except Exception:
        return "%s %s" % (it.get("market"), it.get("selection"))


def _closest(near, k=3):
    out, seen = [], set()
    for it in near:
        g = (it["league"], it["event_id"])
        if g in seen:      # one line per game keeps the list useful
            continue
        seen.add(g)
        need_ev, need_p = settings.MIN_EV, gates.min_win_prob("prematch")
        ev_ok, p_ok = it["ev"] >= need_ev, it["p"] >= need_p
        bits = ["EV <b>%+.1f%%</b>%s %s" % (it["ev"] * 100, "" if ev_ok else " (needs %+.1f%%)" % (need_ev * 100), "\u2705" if ev_ok else "\u274C"),
                "Win prob <b>%.0f%%</b>%s %s" % (it["p"] * 100, "" if p_ok else " (needs %.0f%%)" % (need_p * 100), "\u2705" if p_ok else "\u274C")]
        out.append("\u2022 <b>%s</b> | %s vs %s\n   \U0001F3AF <b>%s</b>\n   %s" % (
            html.escape(str(it["league"]).upper()), html.escape(str(it["home"])), html.escape(str(it["away"])),
            html.escape(_bet_text(it)), " | ".join(bits)))
        if len(out) >= k:
            break
    return out


RULE = "\u2501" * 15


def format_digest(snap: dict, ctx: dict, now: datetime = None) -> str:
    """Telegram HTML. snap: ScanStats.snapshot_and_reset(); ctx: money / credits / health numbers from gather_ctx."""
    now = now or datetime.now(timezone.utc)
    e = html.escape
    L = ["\U0001F4CB <b>BEAST SCAN SUMMARY</b> | " + _wat(now), RULE,
         "Last %d min: <b>%d</b> Pre-Match, <b>%d</b> Sim, <b>%d</b> Live scan(s)" % (
             round(snap.get("window_min", 0)), snap["n_pre"], snap["n_sim"], snap["n_live"]), ""]
    pre = snap.get("pre")
    L.append("\U0001F3C0 <b>PRE-MATCH</b> (latest scan)")
    if pre:
        pct = round(100 * pre["priced"] / pre["games"]) if pre["games"] else 0
        L.append("Games found: <b>%d</b> | Priced by Pinnacle: <b>%d</b> (%d%%)" % (pre["games"], pre["priced"], pct))
        L.append("Qualified and sent: <b>%d</b>" % pre["sent"])
        close = _closest(snap.get("near") or [])
        if close and not pre["sent"]:
            L.append("<i>Closest to qualifying (not bets):</i>")
            L += close
        elif not pre["priced"]:
            L.append("No game had a Pinnacle price, so nothing could be checked.")
        elif not close and not pre["sent"]:
            L.append("No edge close to the rules in any priced game.")
    else:
        L.append("No Pre-Match scan finished yet in this window.")
    L += ["", "\U0001F534 <b>LIVE</b> (this window)"]
    if snap["n_live"]:
        L.append("Live games seen (most at once): <b>%d</b> | Priced by Pinnacle: <b>%d</b> | Sent: <b>%d</b>" % (
            snap["live_games_max"], snap["live_priced_max"], snap["live_sent"]))
    else:
        L.append("No Live scan ran.")
    ol = ctx.get("outlooks")
    if ol:
        from core.quarters import format_outlook
        L += ["", "\U0001F4C8 <b>QUARTER OUTLOOK</b> (live games now)", format_outlook(ol, limit=3)]
    st = ctx.get("stances") or []
    if st:
        L += ["", "\U0001F512 <b>LIVE STANCES</b> (locked, last 6 h)"]
        for lg, h, a, txt, _t in st[:6]:
            L.append("\u2022 %s | %s vs %s: <b>%s</b>" % (e(str(lg).upper()), e(str(h)), e(str(a)), e(txt)))
    pos = ctx.get("positions") or []
    if pos:
        L += ["", "\U0001F4CC <b>PRE-MATCH POSITIONS</b>"] + [e(x) for x in pos]
    lm = ctx.get("live_markets")
    if lm and lm.get("types"):
        per = lm.get("period_markets") or []
        L += ["", "\U0001F50E <b>SportyBet live markets:</b> %d types seen; quarter/half markets live: %s" % (
            lm["types"], ("; ".join("%s (id %s)" % (e(n), i) for i, n, _s in per)) if per else "<b>none</b>")]
    sim = snap.get("sim")
    L += ["", "\U0001F3B2 <b>SIM</b> (latest scan)"]
    L.append("Games: <b>%d</b> | Priced by Pinnacle: <b>%d</b> | Candidates: <b>%d</b> | Sent: <b>%d</b>" % (
        sim["games"], sim["priced"], sim["cands"], sim["sent"]) if sim else "No Sim scan finished yet.")
    by = ctx.get("alerts_by_mode") or {}
    L += ["", RULE, "\U0001F4E8 <b>ALERTS TODAY:</b> Pre-Match <b>%d</b> | Sim <b>%d</b> | Live <b>%d</b>" % (
              by.get("prematch", 0), by.get("sim", 0), by.get("live", 0)),
          "\U0001F4B0 <b>BANKROLL \u20a6%s</b> | Placed today <b>%d</b> | Open bets awaiting /result: <b>%d</b>" % (
              format(int(round(ctx.get("bankroll", 0))), ","), ctx.get("placed_today", 0), ctx.get("open_bets", 0))]
    cov = (pre or {}).get("per_lg") or {}
    if cov:
        L += ["", "\U0001F4E1 <b>Pinnacle coverage:</b> " + " | ".join(
            "%s <b>%d/%d</b>" % (e(k), v[1], v[0]) for k, v in sorted(cov.items()))]
    nxt = (pre or {}).get("upcoming") or []
    if nxt:
        L += ["", "\U0001F552 <b>Next priced kick-offs:</b>"] + [
            "\u2022 %s | %s vs %s" % (_wat(c), e(str(h)), e(str(a))) for c, h, a in nxt[:3]]
    L += ["", "\U0001FA99 Credits today: Odds API <b>%s</b> | OddsPapi <b>%s</b>" % (ctx.get("credits", "?"), ctx.get("papi", "?"))]
    warn = list(ctx.get("warnings") or [])
    for k, v in cov.items():
        if v[0] and not v[1] and k in (ctx.get("keyed") or ()):
            warn.append("No Pinnacle price for %s (%d games)" % (k, v[0]))
    if warn:
        L += [""] + ["\u26a0\ufe0f <b>%s</b>" % e(w) for w in warn]
    L += ["", "<i>Pre-Match every 30 min | Live every 2 min | Summary every %d min</i>" % round(every_min())]
    return "\n".join(L)


def gather_ctx(beast) -> dict:
    """Money, credits and health numbers (DB / cache reads only, no network)."""
    from storage import repository as repo
    from config.leagues import LEAGUE_KEYS, pin_source
    ctx = dict(keyed={lg for lg in LEAGUE_KEYS if pin_source(lg)})
    try:
        rows = repo.picks_today()
        by = {}
        for p in rows:
            by[p["mode"]] = by.get(p["mode"], 0) + 1
        ctx["alerts_by_mode"] = by
        ctx["placed_today"] = sum(1 for p in rows if p["status"] in ("placed", "won", "lost"))
        ctx["open_bets"] = sum(1 for p in repo.placed_bets(500) if p["status"] == "placed")
        ctx["bankroll"] = repo.bankroll_current()
        ctx["last_alert_ago"] = repo.last_alert_ago()
    except Exception as e:
        log.warning("digest money part failed: %s", e)
    pr = getattr(beast, "pinref", None)
    warns = []
    if pr is not None:
        try:
            ctx["credits"] = "%d/%d" % (pr.spent_today(), pr.daily_cap)
            ctx["papi"] = "%d/%d" % (pr._papi_spent_today(), pr.papi_daily_cap)
            for lg, b in pr.data["leagues"].items():
                if b.get("events") and pr._age_min(b) > 1.5 * pr._ttl_min(b) + 30:
                    warns.append("Pinnacle data for %s is %d min old" % (lg, pr._age_min(b)))
        except Exception as e:
            log.warning("digest pinref part failed: %s", e)
    try:   # CLAUDE FIX: how many API keys still have credits this session (a dead key used to be invisible)
        parts = []
        for label, cl in (("Odds API", getattr(pr, "_api", None)), ("OddsPapi", getattr(pr, "_papi", None))):
            keys, dead = list(getattr(cl, "keys", None) or []), set(getattr(cl, "exhausted", None) or [])
            if cl is None and label == "Odds API":   # the client starts at the first fetch: count the configured keys until then
                keys = list(getattr(settings, "ODDS_API_KEYS", None) or [])
            if keys:
                parts.append("%s %d/%d" % (label, len(keys) - len(dead & set(keys)), len(keys)))
                if dead & set(keys):
                    warns.append("%d %s key(s) out of credits this session" % (len(dead & set(keys)), label))
        ctx["keys"] = " \u00B7 ".join(parts)
    except Exception as e:
        log.warning("digest key part failed: %s", e)
    try:   # real monthly OddsPapi quota from the keys' own accounts (a free, unmetered call; cached 3 h)
        cl = getattr(pr, "_papi", None)
        if cl is None:                      # the engine's own client starts at its first pull: use a small one of our own until then
            from services import oddspapi as _op
            cl = _op.quota_client()
        rows = cl.probe_quota_async() if cl is not None and hasattr(cl, "probe_quota_async") else None
        if rows:
            import calendar
            from datetime import datetime, timezone
            n = datetime.now(timezone.utc)
            days_left = max(1, calendar.monthrange(n.year, n.month)[1] - n.day + 1)
            left = sum(l - u for l, u in rows)
            ctx["papi_month"] = "%d keys, %d of %d requests left (~%d a day to month end)" % (
                len(rows), left, sum(l for l, u in rows), left / days_left)
    except Exception as e:
        log.warning("digest quota part failed: %s", e)
    ctx["warnings"] = warns
    try:
        ctx["stances"] = beast.stances.board() if getattr(beast, "stances", None) else []
        from core import positions as _pos
        ctx["positions"] = _pos.summary_lines(beast.positions) if getattr(beast, "positions", None) else []
    except Exception as e:
        log.warning("digest stance/position part failed: %s", e)
    try:
        from core.bookmakers import sportybet as _sb
        ctx["live_markets"] = _sb.live_market_report()
    except Exception:
        ctx["live_markets"] = None
    try:
        ctx["outlooks"] = list(getattr(getattr(beast, "live_scanner", None), "outlooks", []) or [])
    except Exception:
        ctx["outlooks"] = []
    return ctx
