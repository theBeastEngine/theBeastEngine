"""One layout for every Telegram command reply, the same as the Pre-Match and Quarter messages:
a headline, a rule, a one-line plain purpose, an aligned block of the numbers that matter, then only the sections
that have something to say. Pure formatting (no I/O). Everything returned is Telegram HTML, all text is escaped here."""
import html

RULE = "━" * 15


def dots(rows) -> str:
    """[(label, text)] -> aligned 'Label ....: text' lines (monospace block, columns line up in Telegram)."""
    rows = [(str(a), str(b)) for a, b in rows]
    if not rows:
        return ""
    w = max(len(a) for a, _ in rows) + 1
    return "\n".join("%s%s: %s" % (a, " " + "." * (w - len(a)) if w - len(a) > 0 else "", b) for a, b in rows)


def card(emoji: str, title: str, purpose: str = None, rows=None, sections=None, note: str = None) -> str:
    """rows: [(label, value)] shown as an aligned block. sections: [(heading, [lines])]; lines are plain text and are
    escaped unless a line is wrapped in Raw(...). note: one italic line at the bottom."""
    e = html.escape
    out = ["%s <b>%s</b>" % (emoji, e(title)), RULE]
    if purpose:
        out.append("<i>%s</i>" % e(purpose))
    if rows:
        out += ["", "<pre>%s</pre>" % e(dots(rows))]
    for head, lines in sections or []:
        lines = [x for x in (lines or []) if x not in (None, "")]
        if not lines:
            continue
        out += ["", "<b>%s</b>" % e(head)] if head else [""]
        out += [x.html if isinstance(x, Raw) else e(str(x)) for x in lines]
    if note:
        out += ["", "<i>%s</i>" % e(note)]
    return "\n".join(out)


class Raw:
    """A line that is already Telegram HTML."""

    def __init__(self, html_text: str):
        self.html = html_text


def money(x) -> str:
    return "₦" + format(int(round(x or 0)), ",")


def pick_block(p: dict, describe, mlabel) -> list:
    """Two lines for one pick: who/where, then what to bet. Raw HTML lines."""
    e = html.escape
    try:
        what = "%s: %s" % (mlabel(p), describe(p))
    except Exception:
        what = "%s %s" % (p.get("market"), p.get("selection"))
    return [Raw("<b>#%s</b> · %s · %s vs %s" % (p.get("id"), e(str(p.get("league", "")).upper()),
                                                      e(str(p.get("home", ""))), e(str(p.get("away", ""))))),
            Raw("   \U0001F3AF <b>%s</b> @ <b>%.2f</b> · EV <b>%+.1f%%</b> · %s" % (
                e(what), p.get("book_odds") or 0, (p.get("ev") or 0) * 100, e(str(p.get("status", "")).upper())))]
