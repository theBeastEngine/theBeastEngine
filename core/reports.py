"""Hourly reports, split in three jobs (pure formatting + a little state):
  1. GAMES: every game found by the pre-match scan, marked Pinnacle / No Pinnacle data. The first report of the day is
     the full list; later ones say "same as HH:MM" and only list what was added / newly priced / started.
  2. CLOSE CALLS (one per mode: Pre-Match, Live, Sim): markets that are near the bar, not bets. A market has to be near
     on 2 scans in a row to appear and has to be missing for 3 scans in a row to leave, one side per game, so the list
     does not flip on noise. A real move of the Pinnacle line is shown as a move.
  3. ENGINE STATUS: live games, stances, positions, sim, coverage, credits, money."""
import html
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from config.settings import settings
from core import gates

ENTER_SCANS = 2      # near the bar on this many scans in a row before it is shown
LEAVE_SCANS = 3      # missing on this many scans in a row before it is dropped
MAX_GAMES = 8        # games listed per close-calls report
MAX_PER_GAME = 5
RULE = "━" * 15
KV_BASELINE = "report_games_baseline"
MODE_HEAD = {"prematch": "PRE-MATCH", "live": "LIVE", "sim": "SIM"}


def _off() -> float:
    from core.digest import _tz
    return _tz()


def to_local(dt):
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=_off())))


def wat_label() -> str:
    off = _off()
    return "WAT" if off == 1 else "UTC%+g" % off


def stamp(dt) -> str:
    return to_local(dt).strftime("%d %b %H:%M") + " " + wat_label()


def hhmm(ts: float) -> str:
    return to_local(datetime.fromtimestamp(ts, tz=timezone.utc)).strftime("%H:%M")


# ------------------------------------------------------------------ 1. games
def game_key(g: dict) -> str:
    c = g.get("commence")
    return "%s|%s|%s|%s" % (g.get("league"), g.get("home"), g.get("away"), c.strftime("%Y-%m-%dT%H:%M") if hasattr(c, "strftime") else c)


def _line(g: dict) -> str:
    e = html.escape
    when = ""
    if g.get("commence") is not None and hasattr(g["commence"], "strftime"):
        when = " · " + to_local(g["commence"]).strftime("%d %b %H:%M")
    return "%s · %s vs %s%s" % (e(str(g["league"]).upper()), e(str(g["home"])), e(str(g["away"])), when)


def _block(games: list) -> list:
    games = sorted(games, key=lambda g: (g.get("commence") or datetime.max, str(g.get("league"))))
    yes = [g for g in games if g.get("priced")]
    no = [g for g in games if not g.get("priced")]
    out = []
    if yes:
        out += ["✅ <b>Pinnacle</b>"] + [_line(g) for g in yes] + [""]
    if no:
        out += ["❌ <b>No Pinnacle data</b>"] + [_line(g) for g in no] + [""]
    return out


def games_report(games: list, baseline, now: datetime, n_pre=0, n_sim=0, n_live=0, window_min=60) -> tuple:
    """-> (text, new_baseline). baseline: dict from the first report of the local day, or None."""
    today = to_local(now).strftime("%Y-%m-%d")
    clock = to_local(now).strftime("%H:%M")
    cur = {game_key(g): g for g in games}
    n_priced = sum(1 for g in games if g.get("priced"))
    head = ["\U0001F4CB <b>GAMES</b> · %s" % stamp(now), RULE,
            "<i>Every coming game the engine found. \u2705 has a Pinnacle price and is checked for bets; \u274C has none and is skipped.</i>",
            "Last %d min: <b>%d</b> Pre-Match · <b>%d</b> Sim · <b>%d</b> Live scans" % (round(window_min), n_pre, n_sim, n_live)]
    if not games:
        return "\n".join(head + ["", "\U0001F3C0 No games found in the pre-match window."]), baseline
    if not baseline or baseline.get("day") != today:
        text = head + ["", "\U0001F3C0 <b>GAMES FOUND (%d)</b> · Pinnacle price on %d" % (len(games), n_priced), ""] + _block(games)
        return "\n".join(text).rstrip(), dict(day=today, at=clock, games={k: bool(g.get("priced")) for k, g in cur.items()})
    base = dict(baseline.get("games") or {})
    added = [g for k, g in cur.items() if k not in base]
    newly = [g for k, g in cur.items() if k in base and g.get("priced") and not base[k]]
    gone = [k for k in base if k not in cur]
    text = head + ["", "\U0001F3C0 <b>GAMES</b>: same as %s · %d found, Pinnacle price on %d" % (baseline.get("at", "?"), len(games), n_priced)]
    if added:
        text += ["", "➕ <b>ADDED since %s (%d)</b>" % (baseline.get("at", "?"), len(added))] + _block(added)
    if newly:
        text += ["", "➕ <b>NOW PRICED (%d)</b>" % len(newly)] + [_line(g) for g in newly] + [""]
    if gone:
        text += ["", "✔ <b>STARTED or no longer listed (%d)</b>" % len(gone)]
    for k, g in cur.items():
        base[k] = bool(g.get("priced"))
    return "\n".join(text).rstrip(), dict(day=today, at=baseline.get("at", clock), games=base)


# ------------------------------------------------------------------ 2. close calls
def _ckey(it: dict) -> tuple:
    return (it.get("league"), it.get("event_id"), it.get("market"), it.get("selection"))


def closeness(it: dict, mode: str = "prematch") -> str:
    """Big diamond = needs only a little more, small diamond = further away. Same two gates as the alerts (edge, win probability)."""
    need_ev = settings.MIN_EV_LIVE if mode == "live" else settings.MIN_EV
    need_p = gates.min_win_prob(mode)
    gap = max(need_ev - (it.get("ev") or 0.0), 0.0) / max(need_ev, 1e-9) + max(need_p - (it.get("p") or 0.0), 0.0) / 0.10
    return "\U0001F536" if gap <= 0.5 else "\U0001F538"


class CloseCalls:
    """Remembers, per mode, which near-miss markets have held, so the report is steady (see module docstring)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = {"prematch": {}, "live": {}, "sim": {}}
        self.locks = {}     # (mode, league, game, kind, period) -> the side first shown; one side per market per local day

    def _side_ok(self, mode: str, it: dict, now: float) -> bool:
        """A market (e.g. the moneyline of one game) shows ONE side for the whole local day: once Home is listed, Away of
        the same market never replaces it, so the list cannot flip between the two sides."""
        try:
            kind, per = gates.kind_period(it)
        except Exception:
            kind, per = it.get("market"), None
        day = to_local(datetime.fromtimestamp(now, tz=timezone.utc)).strftime("%Y%m%d")
        if self.locks.get("_day") != day:
            self.locks = {"_day": day}
        key = (mode, it.get("league"), it.get("event_id"), kind, per)
        side = self.locks.setdefault(key, it.get("selection"))
        return side == it.get("selection")

    def update(self, mode: str, items: list, now: float = None):
        now = time.time() if now is None else now
        with self._lock:
            st = self.state.setdefault(mode, {})
            seen = set()
            for it in items or []:
                k = _ckey(it)
                if k in seen:
                    continue
                seen.add(k)
                e = st.get(k)
                if e is None:
                    st[k] = dict(item=it, first=now, streak=1, miss=0, shown=False, shown_since=None, note=None)
                    e = st[k]
                else:
                    old = e["item"].get("line")
                    new = it.get("line")
                    if old is not None and new is not None and abs(float(old) - float(new)) > 1e-9:
                        e["note"] = "Pinnacle line moved %g → %g" % (float(old), float(new))
                    e["item"] = it
                    e["streak"] += 1
                    e["miss"] = 0
                if not e["shown"] and e["streak"] >= ENTER_SCANS and self._side_ok(mode, it, now):
                    e["shown"], e["shown_since"] = True, now
            for k in list(st):
                if k not in seen:
                    st[k]["miss"] += 1
                    st[k]["streak"] = 0
                    if st[k]["miss"] >= LEAVE_SCANS:
                        del st[k]

    def entries(self, mode: str) -> list:
        """Shown markets, one side per game (the earliest holder wins), grouped by game."""
        with self._lock:
            shown = sorted((e for e in self.state.get(mode, {}).values() if e["shown"]), key=lambda e: e["shown_since"])
        games, order = {}, []
        for e in shown:
            it = e["item"]
            g = (it.get("league"), it.get("event_id"))
            kept = games.setdefault(g, [])
            if g not in order:
                order.append(g)
            if len(kept) >= MAX_PER_GAME:
                continue
            try:
                if any(gates.conflicts(it, k["item"]) for k in kept):
                    continue
            except Exception:
                pass
            kept.append(e)
        return [(g, games[g]) for g in order if games[g]]


def close_calls_card(cc: CloseCalls, now: datetime) -> str:
    """ONE card for all modes (Pre-Match, Live, Sim), edited in place through the day."""
    L = ["\U0001F3AF <b>CLOSE CALLS</b> \u00B7 %s" % stamp(now), RULE,
         "<i>Not bets: near the rules, not over them. If one passes, it arrives as its own alert.</i>"]
    any_ = False
    for mode in ("prematch", "live", "sim"):
        t = close_calls_report(cc, mode, now, head=False)
        L += ["", "<b>%s</b>" % MODE_HEAD[mode]]
        if t:
            any_ = True
            L.append(t)
        else:
            L.append("Nothing close right now.")
    if any_:
        L += ["", "\U0001F536 close \u00B7 \U0001F538 further away"]
    return "\n".join(L)


def close_calls_report(cc: CloseCalls, mode: str, now: datetime, head: bool = True) -> "str | None":
    """None when there is nothing to show (nothing is sent). head=False: only the game blocks (used inside the one card)."""
    from core.telegram_notify import describe, _mlabel
    e = html.escape
    groups = cc.entries(mode)
    if not groups:
        return None
    L = ["\U0001F3AF <b>CLOSE CALLS · %s</b> · %s" % (MODE_HEAD.get(mode, mode.upper()), stamp(now)), "<i>Not bets: near the rules, not over them. If one passes, it arrives as its own alert.</i>"] if head else []
    for _g, ents in groups[:MAX_GAMES if head else 4]:
        it = ents[0]["item"]
        L += ["", "<b>%s</b> · %s vs %s" % (e(str(it["league"]).upper()), e(str(it["home"])), e(str(it["away"])))]
        for en in ents:
            x = en["item"]
            try:
                what = "%s: %s" % (_mlabel(x), describe(x))
            except Exception:
                what = "%s %s" % (x.get("market"), x.get("selection"))
            note = (" · " + e(en["note"])) if en.get("note") else ""
            L.append("• %s <b>%s</b> · held since %s%s" % (closeness(x, mode), e(what), hhmm(en["shown_since"]), note))
    cap = MAX_GAMES if head else 4
    if len(groups) > cap:
        L += ["", "+%d more game(s)" % (len(groups) - cap)]
    if head:
        L += ["", "\U0001F536 close · \U0001F538 further away"]
    return "\n".join(L).strip("\n")


# ------------------------------------------------------------------ 3. engine status
def _ago(seconds: float) -> str:
    m = int(max(seconds, 0) // 60)
    return "%d min ago" % m if m < 90 else "%dh ago" % round(m / 60)


def status_warnings(snap: dict, ctx: dict) -> list:
    """The real warnings (feed down, a keyed league with no Pinnacle price, ...) as plain strings."""
    cov = ((snap.get("pre") or {}).get("per_lg")) or {}
    warn = list(ctx.get("warnings") or [])
    for k, v in cov.items():
        if v[0] and not v[1] and k in (ctx.get("keyed") or ()):
            warn.append("No Pinnacle price for %s (%d games)" % (k, v[0]))
    return warn


def status_report(snap: dict, ctx: dict, now: datetime) -> str:
    """ENGINE STATUS in the same layout as the Quarter and Pre-Match messages: a header, a rule, a plain one-line purpose,
    an aligned block of the numbers that matter, then only the sections that have something to say."""
    from core.quarters import _dots
    from core.digest import every_min
    e = html.escape
    rows = []
    if snap.get("n_live"):
        rows.append(("Live", "%d game(s) at once \u00B7 %d priced by Pinnacle \u00B7 %d alert(s) sent" % (
            snap["live_games_max"], snap["live_priced_max"], snap["live_sent"])))
    else:
        rows.append(("Live", "no live scan ran"))
    sim = snap.get("sim")
    rows.append(("Sim", "%d games \u00B7 %d priced \u00B7 %d candidates \u00B7 %d sent" % (
        sim["games"], sim["priced"], sim["cands"], sim["sent"]) if sim else "no scan finished yet"))
    last = ctx.get("last_alert_ago")
    rows.append(("Last alert", _ago(last) if last is not None else "none yet"))
    rows.append(("Credits today", "Odds API %s \u00B7 OddsPapi %s" % (ctx.get("credits", "?"), ctx.get("papi", "?"))))
    if ctx.get("keys"):
        rows.append(("Keys with credits", ctx["keys"]))
    if ctx.get("papi_month"):
        rows.append(("OddsPapi month", ctx["papi_month"]))
    rows.append(("Bankroll", "\u20a6%s" % format(int(round(ctx.get("bankroll", 0))), ",")))
    rows.append(("Bets today", "placed %d \u00B7 open %d" % (ctx.get("placed_today", 0), ctx.get("open_bets", 0))))
    L = ["\U0001F4CB <b>ENGINE STATUS</b> \u00B7 %s" % stamp(now), RULE,
         "<i>Is the engine healthy, and what is it watching?</i>", "", "<pre>%s</pre>" % e(_dots(rows))]
    st = ctx.get("stances") or []
    if st:
        L += ["", "\U0001F512 <b>LIVE STANCES</b> (locked, last 6 h)"]
        for lg, h, a, txt, _t in st[:6]:
            L.append("\u2022 %s \u00B7 %s vs %s \u2192 <b>%s</b>" % (e(str(lg).upper()), e(str(h)), e(str(a)), e(txt)))
    pos = ctx.get("positions") or []
    if pos:
        L += ["", "\U0001F4CC <b>OPEN PRE-MATCH BETS</b>"] + [e(x) for x in pos]
    lm = ctx.get("live_markets")
    if lm and lm.get("types"):
        per = []
        seen = set()
        for i, n, _s in (lm.get("period_markets") or []):
            if (i, n) not in seen:
                seen.add((i, n))
                per.append("%s (id %s)" % (e(n), i))
        L += ["", "\U0001F50E SportyBet live: %d market types \u00B7 quarter/half markets: %s" % (lm["types"], "; ".join(per) if per else "<b>none</b>")]
    pre = snap.get("pre") or {}
    cov = pre.get("per_lg") or {}
    if cov:
        L += ["", "\U0001F4E1 <b>Pinnacle coverage</b> (games with a price / games found)",
              " \u00B7 ".join("%s %d/%d" % (e(k), v[1], v[0]) for k, v in sorted(cov.items(), key=lambda kv: (-kv[1][1], kv[0])))]
    warn = status_warnings(snap, ctx)
    if warn:
        L += [""] + ["\u26A0\uFE0F <b>%s</b>" % e(w) for w in warn]
    L += ["", "<i>One card, updated every %d min | Pre-Match every 30 min | Live every 2 min</i>" % round(every_min())]
    return "\n".join(L)
