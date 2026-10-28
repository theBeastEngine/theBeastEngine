"""The daily board: three lists (SportyBet, Pinnacle, Pinnacle + SportyBet) for one 06:00-06:00 day, paged into cards.
Pure formatting and bracket maths (no I/O). A list that is too long for one Telegram message continues on a new card
titled "<List>: 28th October 2026 (Page 2)" that is sent as a reply to the page before, so one tap jumps back."""
from __future__ import annotations

import html
from datetime import timedelta

from tennis import cfg
from tennis.cards import RULE, clock, local

e = html.escape
LISTS = ("sporty", "pin", "both")
TITLE = {"sporty": "SportyBet List", "pin": "Pinnacle List", "both": "Pinnacle + SportyBet List"}
ICON = {"sporty": "🟦", "pin": "🟧", "both": "🟩"}
LIMIT = 3600


def start_hour() -> int:
    return int(cfg.f("TN_BOARD_START_HOUR", 6))


def board_day(t: float) -> str:
    """Date key (YYYYMMDD) of the 06:00-06:00 day that contains t."""
    return (local(t) - timedelta(hours=start_hour())).strftime("%Y%m%d")


def bracket(t: float) -> tuple:
    """(start, end) epoch seconds of the board day that contains t."""
    d = (local(t) - timedelta(hours=start_hour())).replace(hour=0, minute=0, second=0, microsecond=0)
    off = cfg.tz_offset() * 3600
    # local midnight of that date + start hour, turned back into epoch
    start = (d + timedelta(hours=start_hour())).replace(tzinfo=None)
    import calendar
    s = calendar.timegm(start.timetuple()) - off
    return s, s + 86400


def ordinal(n: int) -> str:
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def date_text(t: float) -> str:
    d = local(t) - timedelta(hours=start_hour())
    return "%s %s %d" % (ordinal(d.day), d.strftime("%B"), d.year)


def pretty_key(k: str) -> str:
    return " ".join(w.capitalize() if w not in ("atp", "wta", "itf") else w.upper() for w in str(k).replace("tennis_", "").split("_"))


def paginate(title: str, intro: list, rows: list, limit: int = LIMIT) -> list:
    """rows: list of (text, is_heading); a blank spacer row is (\"\", False). Fills a page until the next row would
    overflow, then starts a new one. A league heading is never left alone at the bottom of a page, and a league that
    carries on over a page break is re-headed with "(cont.)". Returns the page bodies (no title line)."""
    pages, cur, last_head = [], list(intro), None
    fresh = len(cur)

    def size(extra):
        return len("\n".join(cur + extra))

    i = 0
    while i < len(rows):
        text, head = rows[i]
        need = [text]
        if head and i + 1 < len(rows):
            need.append(rows[i + 1][0])            # keep the heading with its first match
        if len(cur) > fresh and size(need) > limit:
            pages.append(cur)
            cur = []
            fresh = 0
            if not head and last_head:
                cur.append("%s <b>(cont.)</b>" % last_head)
            if not text and not head:              # no spacer at the top of a page
                i += 1
                continue
        if head:
            last_head = text
        cur.append(text)
        i += 1
    pages.append(cur)
    return ["\n".join(p) for p in pages]


def render(kind: str, now: float, games: list, mark, extra=None, limit: int = LIMIT) -> list:
    """games: [{lname, home, away, t, id}]. mark(g, now) -> short status icon/text. Returns the page texts, titles included."""
    day = date_text(now)
    by = {}
    for g in games:
        by.setdefault(g["lname"], []).append(g)
    head = "%s <b>%s: %s</b>" % (ICON[kind], TITLE[kind], e(day))
    legend = {
        "sporty": "✅ has a Pinnacle price | ➖ not matched yet | ▶ started",
        "pin": "✅ on SportyBet | ➖ not on SportyBet | ▶ started",
        "both": "🟢 bet signal | 👀 tracker running | ⏳ tracker not started yet | ▶ started",
    }[kind]
    intro_line = "%d matches in %d leagues | %s\n%s" % (len(games), len(by), "06:00 to 06:00 WAT. Updates itself through the day.", legend)
    rows = []
    for lg in sorted(by, key=lambda k: min(g["t"] for g in by[k])):
        rows.append(("", False))
        rows.append(("<b>%s</b> (%d)" % (e(lg), len(by[lg])), True))
        for g in sorted(by[lg], key=lambda x: x["t"]):
            rows.append(("%s %s | %s vs %s%s" % (mark(g, now), clock(g["t"]), e(g["home"]), e(g["away"]), (extra(g, now) if extra else "")), False))
    if not games:
        rows = [("No matches listed yet for this day.", False)]
    bodies = paginate(head, ["", intro_line], rows, limit=limit - 200)
    n = len(bodies)
    out = []
    for i, body in enumerate(bodies, 1):
        title = "%s <b>%s: %s%s</b>" % (ICON[kind], TITLE[kind], e(day), " (Page %d)" % i if n > 1 else "")
        foot = []
        if i > 1:
            foot.append("⬆ Tap the quoted message above to jump back to Page %d." % (i - 1))
        if i < n:
            foot.append("⬇ Continues on Page %d." % (i + 1))
        out.append("\n".join([title, RULE, body.strip("\n")] + ([""] + foot if foot else [])))
    return out


def empty_page(kind: str, now: float, n: int) -> str:
    return "%s <b>%s: %s (Page %d)</b>\n(this page is now empty)" % (ICON[kind], TITLE[kind], e(date_text(now)), n)
