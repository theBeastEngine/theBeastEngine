"""Price table on the Telegram match cards, shared by every sport (tennis, football, and any sport added later).

One row per outcome: short name, SportyBet price, Pinnacle fair price, edge. Rows are at most 28 characters wide (the most a phone shows in a code block at a large font size) so the
edge never wraps onto the next line in a phone chat bubble, and the three columns line up.
"""
from __future__ import annotations

import html
import re

WIDTH = 12          # longest label ever asked for
LINE = 28           # widest row that fits on one line of a phone chat bubble
_KEEP = ("Over", "Under", "Odd", "Even", "Draw")
_GENERIC = ("united", "city", "town", "rovers", "athletic", "wanderers", "state", "club", "university")   # not a team name on their own


def short(name: str, n: int = WIDTH) -> str:
    """Fit a label into n characters; a trailing line such as +0.5 is kept and the name is shortened instead."""
    name = (name or "").strip()
    if len(name) <= n:
        return name
    m = re.search(r"\s([+-]?\d+(?:\.\d+)?)$", name)
    if m and len(m.group(0)) < n - 3:
        tail = m.group(0)
        return name[:n - len(tail) - 1].rstrip() + "." + tail
    return name[:n - 1] + "."


def first_name(label: str, last: bool = False) -> str:
    """First word of a player or team name (tags of up to 3 letters such as CR, EC, MG, de, van are skipped), plus any trailing line or score.
    last=True takes the last long word instead: basketball teams are known by the nickname (Los Angeles Lakers -> Lakers)."""
    lab = (label or "").strip()
    tok = lab.split()
    if len(tok) < 2 or tok[0] in _KEEP or tok[0][:1].isdigit():
        return lab
    tail = tok[-1] if re.fullmatch(r"[+-]?\d+(?:[.:]\d+)?", tok[-1]) else ""
    words = tok[:-1] if tail else tok
    words = [w for w in words if len(w) > 3] or words
    w = words[0]
    if last:
        w = words[-1] if (words[-1].lower() not in _GENERIC or len(words) == 1) else words[0]
    return w + (" " + tail if tail else "")


def _tail(f: str) -> str:
    m = re.search(r"\s+([+-]?[\d.:]+)$", f)
    return " " + m.group(1) if m else ""


def labels(rows: list, last: bool = False) -> list:
    """Short label for each row of one market. If two rows share the same first word (Manchester United / Manchester City), add the next word's initial."""
    firsts = [first_name(r[0], last) for r in rows]
    bare = [re.sub(r"\s+[+-]?[\d.:]+$", "", f) for f in firsts]
    fulls = [(r[0] or "").strip() for r in rows]
    if len(rows) > 1 and len(set(bare)) < len(set(fulls)):
        out = []
        for f, w, full in zip(firsts, bare, fulls):
            rest = [x for x in re.sub(r"\s+[+-]?[\d.:]+$", "", full).split()[1:] if len(x) > 3]
            out.append((w[:WIDTH - 3] + " " + rest[0][0] + "." if rest else w) + _tail(f))
        return [short(x) for x in out]
    return [short(f) for f in firsts]


def table_text(table: list, last: bool = False) -> list:
    L = []
    for head, rows in table or []:
        L.append("<b>%s</b>" % head)
        odds = ["%.2f" % r[1] for r in rows]
        fair = [("%.2f" % r[2]) if r[2] else "-" for r in rows]
        edge = [("%+.1f%%" % (100 * r[3])) if r[3] is not None else "-" for r in rows]
        wo, wf, we = max(map(len, odds)), max(map(len, fair)), max(map(len, edge))
        wl = max(6, min(WIDTH, LINE - (wo + wf + we + 3)))        # the name gets whatever room the numbers leave
        t = ["%-*s %*s|%*s|%*s" % (wl, short(lab, wl), wo, o, wf, f, we, g) for lab, o, f, g in zip(labels(rows, last), odds, fair, edge)]
        L.append("<pre>%s</pre>" % html.escape("\n".join(t)))
    return L
