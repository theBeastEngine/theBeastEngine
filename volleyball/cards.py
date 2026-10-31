"""Telegram text for volleyball: match card, verdict message, agenda, status. Pure formatting (no I/O)."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from core import table as _table
from volleyball import cfg, model

RULE = "━" * 15
LATE = ("t60", "final", "t15", "last")
LATE_NAME = {"t60": "T-60", "final": "T-30", "t15": "T-15", "last": "T-5"}
e = html.escape


def local(t: float) -> datetime:
    return datetime.fromtimestamp(t, tz=timezone.utc) + timedelta(hours=cfg.tz_offset())


def clock(t: float) -> str:
    return local(t).strftime("%H:%M")


def tip_text(t: float) -> str:
    lbl = "WAT" if cfg.tz_offset() == 1 else "UTC%+g" % cfg.tz_offset()
    return "%s %s" % (local(t).strftime("%a %d %b %H:%M"), lbl)


def plan_slots(now: float, tip: float) -> list:
    """Checks spread between now and T-65, then T-60, T-30 (FINAL), T-15 and T-5. Slots already in the past are left out."""
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
    if now >= t30 and slots and slots[0]["kind"] == "check":
        slots[0]["label"], slots[0]["kind"] = "FINAL Check", "final"
    if tip - 900 > now + 120 and cfg.on("VB_T15_CHECK"):
        slots.append(dict(t=tip - 900, label="Check: T-15", kind="t15", done=False))
    if tip - 300 > now + 120 and cfg.on("VB_LAST_LOOK"):
        slots.append(dict(t=tip - 300, label="LAST LOOK: T-5", kind="last", done=False))
    return slots


def row(c: dict, home: str, away: str) -> str:
    return "%s | SportyBet %.2f | fair %.2f | edge %+.0f%% | win chance %.0f%%" % (
        e(model.describe(c, home, away)), c["odds"], c["fair_odds"], 100 * c["ev"], 100 * c["p"])


def _fmt_ml(x):
    return "%.2f / %.2f" % (x["home"], x["away"]) if x else "-"


def lines_block(m: dict, chk: dict) -> list:
    L = []
    s, p = chk.get("soft_ml"), chk.get("pin_ml")
    if p is None and chk["status"] == "nopin":
        L.append("Pinnacle: no price for this game (reported only, never a bet)")
    else:
        L.append("Pinnacle moneyline: %s%s" % (_fmt_ml(p), (" (price %.0f min old)" % chk["age"]) if chk.get("age") is not None else ""))
    L.append("SportyBet moneyline: %s" % _fmt_ml(s))
    return L


FAMILY = (("ml", "MONEYLINE (match winner)"), ("ah", "SET HANDICAP (closest line)"), ("total", "TOTAL SETS (closest line)"))


def _short(name: str, n: int = 16) -> str:
    name = (name or "").strip()
    if len(name) <= n:
        return name
    m = re.search(r"\s([+-]?\d+(?:\.\d+)?)$", name)      # keep a trailing line such as +0.5 and shorten the name instead
    if m and len(m.group(0)) < n - 3:
        tail = m.group(0)
        return name[:n - len(tail) - 1].rstrip() + "." + tail
    return name[:n - 1] + "."


def compare_table(cands: list, home: str, away: str, soft_ml=None) -> list:
    """[(heading, [(label, SportyBet odds, Pinnacle fair odds or None, edge or None)])] for the closest line of each market family."""
    out = []
    order = {"home": 0, "away": 1, "over": 0, "under": 1}
    for fam, head in FAMILY:
        cs = [c for c in cands if c["mkt"] == fam]
        if not cs:
            if fam == "ml" and soft_ml:
                out.append((head, [(home, soft_ml["home"], None, None), (away, soft_ml["away"], None, None)]))
            continue
        best = max(cs, key=lambda c: c["ev"])
        same = cs if fam == "ml" else [c for c in cs if c["line"] == best["line"]]
        rows = []
        for c in sorted(same, key=lambda c: order.get(c["sel"], 3)):
            d = model.describe(c, home, away).replace(" to win the match", "").replace(" sets", "")
            rows.append((d, c["odds"], c["fair_odds"], c["ev"]))
        out.append((head, rows))
    return out


def _have(mk: dict) -> str:
    names = {"ml": "moneyline", "ah": "set handicap", "total": "total sets"}
    return ", ".join(names[k] for k in ("ml", "ah", "total") if mk.get(k)) or "nothing"


def plain_result(cands: list, elig: list, home: str, away: str, soft: dict = None, pin: dict = None) -> str:
    """One plain sentence: what the engine concludes and how far the closest market is from a bet."""
    if elig:
        return "POSSIBLE BET: %s. Needs a second check to confirm." % model.describe(elig[0], home, away)
    gap = [c for c in cands if c["why_not"]]
    if not gap:
        if soft is not None and pin is not None:
            return "NO BET. Nothing comparable on both books yet. SportyBet has: %s. Pinnacle has: %s." % (_have(soft), _have(pin))
        return "NO BET. Nothing comparable on both books yet."
    c = max(gap, key=lambda c: c["ev"])
    what = "%s (SportyBet %.2f, Pinnacle fair %.2f)" % (model.describe(c, home, away), c["odds"], c["fair_odds"])
    if c["why_not"].startswith("edge") and c["ev"] < cfg.min_ev():
        return "NO BET. Closest to a bet: %s. Edge %+.1f%%, that is %.1f points short of the %.0f%% needed." % (
            what, 100 * c["ev"], 100 * (cfg.min_ev() - c["ev"]), 100 * cfg.min_ev())
    return "NO BET. Closest to a bet: %s. Edge %+.1f%% but blocked: %s." % (what, 100 * c["ev"], c["why_not"])


def table_text(table: list) -> list:
    return _table.table_text(table)


def chances(table: list) -> list:
    """One plain line per market on the card: Pinnacle's chance of each outcome (1/fair odds, rescaled to 100% within the market)."""
    out = []
    for head, rows in table or []:
        rs = [r for r in rows if r[2]]
        if len(rs) != len(rows) or len(rs) < 2:
            continue
        tot = sum(1 / r[2] for r in rs)
        out.append("%s: %s" % (head.split(" (")[0].capitalize(),
                               ", ".join("%s %.0f%%" % (r[0], 100 * (1 / r[2]) / tot) for r in rs)))
    return out


def match_read(table: list) -> str:
    """One plain sentence from Pinnacle's fair prices: favourite and chance, and what the sets line says about scoring."""
    t = {h.split(" (")[0]: rows for h, rows in (table or [])}
    x = t.get("MONEYLINE") or []
    if len(x) != 2 or not all(r[2] for r in x):
        return ""
    ps = [1 / r[2] for r in x]
    ps = [v / sum(ps) for v in ps]
    fav = 0 if ps[0] >= ps[1] else 1
    if ps[fav] >= 0.58:
        out = ["%s is the favourite, about %.0f%% to win " % (x[fav][0], 100 * ps[fav])]
    else:
        out = ["a closely matched game: %s %.0f%%, %s %.0f%%" % (x[0][0], 100 * ps[0], x[1][0], 100 * ps[1])]
    tg = t.get("TOTAL SETS") or []
    mt = re.search(r"(\d+(?:\.\d+)?)$", tg[0][0]) if tg else None
    if mt and float(mt.group(1)) <= 5.5:
        ln = float(mt.group(1))
        out.append("a sets line of %s points to a %s" % (mt.group(1), "short match (3 sets or fewer)" if ln <= 3.5 else ("long match (4 or 5 sets)" if ln >= 5.0 else "match of 3 or 4 sets")))
    return "; ".join(out) + ". (Pinnacle's view, not a tip.)"


def block(m: dict, chk: dict, first: bool, change: list) -> list:
    st = chk["status"]
    home, away = e(m["home"]), e(m["away"])
    L = ["<b>%s · %s</b>" % (e(chk["label"]), clock(chk["t"]))]
    if st == "play" and chk["kind"] in LATE:
        for k in m.get("play_keys", []):
            meta = m["picks"].get(k) or {}
            now_by = {model.pkey(c): c for c in chk.get("rows", [])}
            if k in now_by:
                c = now_by[k]
                L.append("Still holding: <b>%s</b> now %.2f | %+.0f%%" % (e(meta.get("desc", k)), c["odds"], 100 * c["ev"]))
            else:
                L.append("%s: edge gone" % e(meta.get("desc", k)))
        if change:
            L.append("Moved: " + e(" ".join(change)))
        return L
    if st == "play":
        L.append("<b>\U0001F514 PLAY NOW</b>")
        for i, c in enumerate(chk.get("rows", []), 1):
            L.append("%d. <b>%s</b>" % (i, row(c, m["home"], m["away"])))
            L.append("   play only at %.2f or better" % (c["fair_odds"] * (1 + cfg.min_ev())))
        L.append("<b>Recommendation: place before SportyBet adjusts.</b>")
    else:
        label = {"watch": "WATCH (no edge yet)", "seen": "WATCH (edge seen, waiting for a second check)",
                 "hold": "ON HOLD (%s)" % e(str(chk.get("held"))), "nopin": "NO PINNACLE PRICE (reported only)"}[st]
        L.append("Status: " + label)
        if st in ("watch", "seen") and chk.get("result"):
            L.append("<b>%s</b>" % e(chk["result"]))
            L.append("<i>Price columns: SportyBet | Pinnacle fair | edge</i>")
            L += table_text(chk.get("table"))
            if chk.get("age") is not None:
                L.append("Pinnacle price %.0f min old" % chk["age"])
            ch = chances(chk.get("table"))
            if ch:
                L.append("<b>Pinnacle's chances</b>")
                L += ["\u2022 " + e(x) for x in ch]
            rd = match_read(chk.get("table"))
            if rd:
                L.append("<i>Read: %s</i>" % e(rd))
        if chk.get("stale") is not None and chk["kind"] in LATE:
            L.append("<b>Not confirming: the Pinnacle price is %s min old.</b>" % ("over 150" if chk["stale"] > 150 else "%.0f" % chk["stale"]))
        if st != "nopin" and not (st in ("watch", "seen") and chk.get("result")):
            L += lines_block(m, chk)
        if st == "seen" and not chk.get("result"):
            L.append("<b>Possible edge: %s. Waiting for a second check.</b>" % e(chk.get("hint", "")))
        if st in ("watch", "seen") and chk.get("why") and not chk.get("result"):
            L.append("<b>Closest markets</b>")
            L += ["• " + e(x) for x in chk["why"]]
    for a in chk.get("away") or []:
        L.append("<b>Pinnacle moved away from %s: the edge is fading.</b>" % e(a))
    if change:
        L.append("Moved: " + e(" ".join(change)))
    if chk.get("nxt"):
        L.append("Next check: %s" % clock(chk["nxt"]))
    return L


def summary(m: dict) -> list:
    checks = m.get("checks") or []
    pin_i = next((i for i, c in enumerate(checks) if c.get("status") != "nopin"), None)
    if pin_i is None:
        pin = "No Pinnacle price so far (reported only, never a bet)" if checks else "not checked yet"
    elif pin_i == 0:
        pin = "Yes, from the start"
    else:
        pin = "Yes, from check %d (%s)" % (pin_i + 1, clock(checks[pin_i]["t"]))
    L = ["<b>SUMMARY</b>", "Pinnacle: %s" % pin]
    take = []
    for kind in LATE:
        nm = LATE_NAME[kind]
        c = next((c for c in checks if c.get("kind") == kind), None)
        if c is None:
            slot = next((s for s in m.get("slots", []) if s.get("kind") == kind), None)
            L.append("%s: %s" % (nm, ("pending, %s" % clock(slot["t"])) if slot and not m.get("skipped") else "not planned"))
            continue
        now_by = {model.pkey(x): x for x in (c.get("rows") or [])}
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
            word, take = "Skip (edge gone)", []
        else:
            word, take = "Skip (no edge)", []
        L.append("%s: %s" % (nm, word))
    if take:
        L.append("<b>Markets to take</b>")
        for k, c in take:
            meta = m["picks"].get(k) or {}
            L.append("• %s @ %.2f (play at %.2f or better)" % (e(meta.get("desc", k)), c["odds"], meta.get("min_odds") or 0))
    return L


def render(m: dict, max_chars: int = 3800) -> str:
    head = ["<b>MATCH TRACKER</b>", e(m["lname"]), "<b>%s</b> vs" % e(m["home"]), "<b>%s</b>" % e(m["away"]), tip_text(m["tip"])]
    blocks, prev = [], None
    for i, c in enumerate(m["checks"]):
        blocks.append(block(m, c, i == 0, c.get("change") or []))
    tail = "\n" + RULE + "\n" + "\n".join(summary(m))
    def join():
        return "\n".join(["\n".join(head)] + [RULE + "\n" + "\n".join(b) for b in blocks]) + tail
    out, n = join(), 0
    while len(out) > max_chars and n < len(blocks) - 2:
        b = blocks[n]
        blocks[n] = [b[0] + " · " + m["checks"][n]["status"].upper()]
        n += 1
        out = join()
    return out


def verdict(m: dict, chk: dict) -> str:
    name = LATE_NAME.get(chk["kind"], chk["label"])
    now_by = {model.pkey(c): c for c in chk.get("rows", [])}
    stale = chk.get("stale")
    L = ["\U0001F514 <b>%s VERDICT</b> | %s v %s | %s | tip %s" % (name, e(m["home"]), e(m["away"]), e(m["lname"]), clock(m["tip"])), ""]
    play = 0
    for k in m.get("play_keys", []):
        meta = m["picks"].get(k) or {}
        desc = e(meta.get("desc", k))
        if k in now_by and stale is None:
            c = now_by[k]
            L.append("✅ <b>PLAY</b> %s | odds %.2f | edge %+.0f%%" % (desc, c["odds"], 100 * c["ev"]))
            L.append("    play only at %.2f or better" % (meta.get("min_odds") or 0))
            play += 1
        elif k in now_by:
            L.append("⚠️ <b>NOT CONFIRMED</b> %s | Pinnacle price too old to trust" % desc)
        else:
            L.append("❌ <b>SKIP</b> %s | edge gone" % desc)
    for a in chk.get("away") or []:
        L.append("⚠️ Pinnacle moved away from %s" % e(a))
    if chk.get("change"):
        L.append("Moved: " + e(" ".join(chk["change"])))
    n = len(m.get("play_keys", []))
    L.append("")
    if stale is not None:
        L.append("<b>BOTTOM LINE: do not play on this check, the Pinnacle price could not be refreshed.</b>")
    elif play:
        L.append("<b>BOTTOM LINE: play %d of %d market%s.</b>" % (play, n, "" if n == 1 else "s"))
    else:
        L.append("<b>BOTTOM LINE: skip this match, no market still holds.</b>")
    if chk.get("nxt"):
        L.append("Next check: %s" % clock(chk["nxt"]))
    return "\n".join(L)


def agenda(now: float, games: list, priced, limit: int = 3700) -> list:
    """games: [{lid, lname, home, away, t, id}]. League by league. ✅ Pinnacle price, ➖ none yet, ▶ started."""
    head = "⚽ <b>VOLLEYBALL: NEXT 24 HOURS</b> | %s" % e(local(now).strftime("%a %d %b"))
    if not games:
        return [head + "\n" + RULE + "\nNo volleyball matches found."]
    by = {}
    for g in games:
        by.setdefault(g["lname"], []).append(g)
    upcoming = [g for g in games if g["t"] > now]
    ok = sum(1 for g in upcoming if priced(g))
    intro = [head, RULE, "%d matches in %d leagues | not started: %d, with a Pinnacle price: %d. Updates itself through the day; "
             "every priced match has its own tracker card.\n✅ Pinnacle price | ➖ no price yet | ▶ started" % (len(games), len(by), len(upcoming), ok)]
    msgs, cur = [], list(intro)
    for lg in sorted(by, key=lambda k: min(g["t"] for g in by[k])):
        rows = ["", "<b>%s</b> (%d)" % (e(lg), len(by[lg]))]
        for g in sorted(by[lg], key=lambda x: x["t"]):
            mark = "▶" if g["t"] <= now else ("✅" if priced(g) else "➖")
            rows.append("%s %s | %s vs %s" % (mark, clock(g["t"]), e(g["home"]), e(g["away"])))
        if len("\n".join(cur + rows)) > limit and len(cur) > len(intro):
            msgs.append("\n".join(cur))
            cur = [head + " (cont.)", RULE]
        cur += rows
    msgs.append("\n".join(cur))
    return msgs
