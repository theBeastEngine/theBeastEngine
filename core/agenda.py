"""Morning list: every match of the next 24 hours, league by league, with its kick-off time (Lagos) and whether Pinnacle
has a price for it. Sent once a day after the 08:00 report (AGENDA=0 turns it off; AGENDA_HOUR defaults to the report hour).
Each of these matches then gets its own Match Tracker card and sits in its 3-hour window card (core/blocks.py)."""
from __future__ import annotations

import html
import os


def enabled() -> bool:
    return os.environ.get("AGENDA", "1").strip() != "0"


def hour() -> int:
    try:
        return int(os.environ.get("AGENDA_HOUR", os.environ.get("DAY_REPORT_HOUR", 8)))
    except ValueError:
        return 8


def due(now: float, last_day) -> bool:
    from core.tracker import _local
    d = _local(now)
    return enabled() and d.hour >= hour() and last_day != d.strftime("%Y%m%d")


def build(now: float, events, priced, limit: int = 3800) -> list:
    """events: [(league, ev)] ; priced(ev) -> bool. Returns one or more message texts, league by league, kick-off order."""
    from config.leagues import LEAGUES
    from core.tracker import _local, _utc_epoch, clock_text
    from utils.dates import commence_dt
    e = html.escape
    by = {}
    for lg, ev in events:
        try:
            dt = commence_dt(ev.get("commence"))
            if not dt:
                continue
            t = _utc_epoch(dt)
        except Exception:
            continue
        if t <= now or t > now + 24 * 3600:
            continue
        by.setdefault(lg, []).append((t, ev))
    total = sum(len(v) for v in by.values())
    ok = sum(1 for v in by.values() for _t, ev in v if priced(ev))
    head = "\U0001F4C5 <b>MATCHES, NEXT 24 HOURS</b> | %s" % e(_local(now).strftime("%a %d %b"))
    if not total:
        return [head + "\n" + "━" * 15 + "\nNo matches found in the leagues the engine follows."]
    intro = [head, "━" * 15, "%d matches in %d leagues | Pinnacle price on %d. Every match gets its own tracker card; "
             "✅ = Pinnacle price found, ➖ = not yet." % (total, len(by), ok)]
    msgs, cur = [], list(intro)
    for lg in sorted(by, key=lambda k: min(t for t, _ in by[k])):
        rows = ["", "<b>%s</b> (%d)" % (e(LEAGUES.get(lg, {}).get("name", lg.upper())), len(by[lg]))]
        for t, ev in sorted(by[lg], key=lambda x: x[0]):
            day = _local(t).strftime("%a") if _local(t).date() != _local(now).date() else "today"
            rows.append("%s %s %s | %s vs %s" % ("✅" if priced(ev) else "➖", day, clock_text(t), e(str(ev.get("home"))), e(str(ev.get("away")))))
        if len("\n".join(cur + rows)) > limit and len(cur) > len(intro):
            msgs.append("\n".join(cur))
            cur = [head + " (cont.)", "━" * 15]
        cur += rows
    msgs.append("\n".join(cur))
    return msgs


# ---- the day's card: created at 08:00, then edited all day (new games added, prices ticked, started games marked)
def gkey(lg, home, away) -> str:
    from core.bookmakers.soft_feed import norm
    return "%s|%s|%s" % (lg, norm(home), norm(away))


def collect(now: float, events, games: list) -> list:
    """games: the day's list so far [{lg, home, away, t, c}]. Adds every event that starts within the next 24 h and is not in
    the list yet. A game is never removed during the day."""
    from core.tracker import _utc_epoch
    from utils.dates import commence_dt
    have = {gkey(g["lg"], g["home"], g["away"]) for g in games}
    out = list(games)
    for lg, ev in events:
        try:
            dt = commence_dt(ev.get("commence"))
            if not dt:
                continue
            t = _utc_epoch(dt)
        except Exception:
            continue
        if t <= now or t > now + 24 * 3600:
            continue
        k = gkey(lg, ev.get("home"), ev.get("away"))
        if k in have:
            continue
        have.add(k)
        out.append(dict(lg=lg, home=str(ev.get("home")), away=str(ev.get("away")), t=t, c=ev.get("commence")))
    return out


def render(now: float, games: list, priced, limit: int = 3700) -> list:
    """The card text(s), league by league. priced(game) -> bool. Marks: ✅ Pinnacle price, ➖ none yet, ▶ started."""
    from config.leagues import LEAGUES
    from core.tracker import _local, clock_text
    e = html.escape
    head = "\U0001F4C5 <b>MATCHES, NEXT 24 HOURS</b> | %s" % e(_local(now).strftime("%a %d %b"))
    if not games:
        return [head + "\n" + "━" * 15 + "\nNo matches found in the leagues the engine follows."]
    by = {}
    for g in games:
        by.setdefault(g["lg"], []).append(g)
    upcoming = [g for g in games if g["t"] > now]
    ok = sum(1 for g in upcoming if priced(g))
    intro = [head, "━" * 15,
             "%d matches in %d leagues | not started: %d, with a Pinnacle price: %d. This card updates itself through the day. "
             "Every match has its own tracker card.\n✅ Pinnacle price | ➖ no price yet | ▶ started" % (len(games), len(by), len(upcoming), ok)]
    msgs, cur = [], list(intro)
    for lg in sorted(by, key=lambda k: min(g["t"] for g in by[k])):
        rows = ["", "<b>%s</b> (%d)" % (e(LEAGUES.get(lg, {}).get("name", lg.upper())), len(by[lg]))]
        for g in sorted(by[lg], key=lambda x: x["t"]):
            if g["t"] <= now:
                mark = "▶"
            else:
                mark = "✅" if priced(g) else "➖"
            day = _local(g["t"]).strftime("%a") if _local(g["t"]).date() != _local(now).date() else "today"
            rows.append("%s %s %s | %s vs %s" % (mark, day, clock_text(g["t"]), e(g["home"]), e(g["away"])))
        if len("\n".join(cur + rows)) > limit and len(cur) > len(intro):
            msgs.append("\n".join(cur))
            cur = [head + " (cont.)", "━" * 15]
        cur += rows
    msgs.append("\n".join(cur))
    return msgs
