"""Time-block cards: every PLAY NOW pick of the day, grouped by the 3-hour window its game starts in (Lagos time):
6-9am, 9am-12, 12-3pm, 3-6pm, 6-9pm, 9pm-12am, 12-3am, 3-6am. One Telegram message per block, edited in place like the
Match Tracker, plus ONE suggested ticket line (best 2-3 picks from different games, each with a win chance >= 60%).
Env: BLOCKS=0 turns it off, TICKET_MIN_P (0.60), TICKET_MAX_LEGS (3)."""
from __future__ import annotations

import hashlib
import html
import json
import os
import time

from core import gates
from core.reasons import pick_reason
from services.logger import get_logger

log = get_logger("blocks")
KEY = "day_blocks"


def enabled() -> bool:
    return os.environ.get("BLOCKS", "1").strip() != "0"


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _hour_label(h: int) -> str:
    h %= 24
    return "%d%s" % (h % 12 or 12, "am" if h < 12 else "pm")


def block_of(epoch: float):
    """(key, label) of the 3-hour window (local time) a kick-off falls in."""
    from core.tracker import _local
    d = _local(epoch)
    idx = ((d.hour - 6) % 24) // 3
    start = (6 + 3 * idx) % 24
    return "%s|%02d" % (d.strftime("%Y%m%d"), start), "%s–%s" % (_hour_label(start), _hour_label(start + 3)), d.strftime("%a %d %b")


def block_start_epoch(key: str) -> float:
    """Epoch of the start of a block key 'YYYYMMDD|HH' (Lagos clock, ALERT_TZ_OFFSET_HOURS)."""
    from datetime import datetime, timedelta, timezone
    day, hh = key.split("|")
    tz = timezone(timedelta(hours=_f("ALERT_TZ_OFFSET_HOURS", 1)))
    return datetime.strptime(day, "%Y%m%d").replace(hour=int(hh), tzinfo=tz).timestamp()


def match_line(m: dict) -> list:
    """Two short lines about a match that has no PLAY NOW: where it stands now and the closest market with its reason."""
    from core.tracker import clock_text
    e = html.escape
    chk = (m.get("checks") or [None])[-1]
    st = chk.get("status") if chk else None
    mark = "\u2796" if st == "nopin" else ("\u2705" if st else "\u23F3")   # priced by Pinnacle / not priced / not checked yet
    head = "%s %s | <b>%s</b> vs <b>%s</b> (%s)" % (mark, clock_text(m["tip"]), e(m["home"]), e(m["away"]), e(str(m["league"]).upper()))
    if not chk:
        nxt = [s["t"] for s in m.get("slots", []) if not s.get("done")]
        return [head, "   First check at %s" % (clock_text(nxt[0]) if nxt else "soon")]
    word = {"watch": "watching, no edge", "seen": "edge seen once, waiting for a second check", "play": "PLAY NOW above",
            "hold": "on hold (%s)" % e(str(chk.get("held"))), "nopin": "no Pinnacle price, cannot be checked yet"}.get(st, "watching")
    if chk.get("stale") is not None and chk.get("kind") in ("t60", "final", "t15", "last"):
        word += " | Pinnacle price too old to confirm"
    out = [head]
    nxt = chk.get("nxt")
    out.append("   %s%s" % (word.capitalize() if st != "play" else word, (" | next check %s" % clock_text(nxt)) if nxt else ""))
    why = (chk.get("why") or [None])[0]
    if why and st != "play":
        out.append("   %s" % e(why if len(why) <= 120 else why[:117] + "..."))
    return out


def active_picks(m: dict) -> list:
    """[(pick_view, still_holding)] of one match, in PLAY NOW order."""
    from core.tracker import MatchTracker
    mk = MatchTracker._mk
    chk = (m.get("checks") or [None])[-1]
    now_by = {mk(p): p for p in (chk.get("rows") if chk else []) or []}
    out = []
    for k in m.get("play_keys", []):
        meta = (m.get("picks") or {}).get(k) or {}
        if k in now_by:
            out.append((dict(now_by[k], desc=meta.get("desc", k), pk=k), True))
        else:
            out.append((dict(meta.get("last") or {}, desc=meta.get("desc", k), pk=k), False))
    return out


def ticket(groups) -> dict | None:
    """groups: [(match, [live pick views])]. Best 2-3 picks from DIFFERENT games with a win chance >= TICKET_MIN_P."""
    min_p = _f("TICKET_MIN_P", 0.60)
    legs = []
    for m, picks in groups:
        ok = [p for p in picks if p.get("p") and p["p"] >= min_p - 1e-9 and p.get("odds")]
        if ok:
            legs.append((max(ok, key=gates.score), m))
    legs.sort(key=lambda t: gates.score(t[0]), reverse=True)
    legs = legs[:int(_f("TICKET_MAX_LEGS", 3))]
    if len(legs) < 2:
        return None
    odds = prob = 1.0
    for p, _m in legs:
        odds *= p["odds"]
        prob *= p["p"]
    return dict(legs=legs, odds=odds, p=prob, ev=prob * odds - 1)


def render(label: str, day: str, groups: list, others: list = (), max_chars: int = 3800) -> str:
    from core import telegram_notify as tn
    from core.tracker import clock_text
    e = html.escape
    L = ["<b>%s</b>" % ("PLAY NOW LIST" if groups else "MATCH WINDOW: NO PLAY NOW YET"), "<b>%s</b> | %s" % (e(label), e(day))]
    live_groups = []
    for m, picks in groups:
        L += ["", "<b>%s</b> vs <b>%s</b> (%s) | %s" % (e(m["home"]), e(m["away"]), e(m["league"].upper()), clock_text(m["tip"]))]
        live = []
        for i, (p, holding) in enumerate(picks, 1):
            line = "%s | %.0f%% | odds %.2f | edge %+.0f%%" % (e(p.get("desc", "")), 100 * (p.get("p") or 0), p.get("odds") or 0,
                                                                100 * (p.get("ev") or 0))
            if holding:
                L.append("%d. <b>%s</b>" % (i, line))
                why = pick_reason(p)
                if why:
                    L.append("   " + e(why))
                live.append(p)
            else:
                L.append("%d. <s>%s</s> edge gone" % (i, line))
        live_groups.append((m, live))
    t = ticket(live_groups)
    L.append("")
    if not groups:
        L.append("<i>No PLAY NOW in this window yet. Every match below keeps being checked; a PLAY NOW appears here when one qualifies.</i>")
    elif t:
        L.append("<b>Suggested ticket (%d legs)</b>" % len(t["legs"]))
        for i, (p, m) in enumerate(t["legs"], 1):
            L.append("%d. %s | %s vs %s @ %.2f" % (i, e(p.get("desc", "")), e(m["home"]), e(m["away"]), p["odds"]))
        L.append("Combined odds %.2f | win chance %.1f%% | edge %+.0f%%" % (t["odds"], 100 * t["p"], 100 * t["ev"]))
        L.append("<i>A ticket wins only if every leg wins.</i>")
    else:
        L.append("<i>No ticket yet: it needs 2 picks from different games, each with a win chance of at least %.0f%%.</i>"
                 % (100 * _f("TICKET_MIN_P", 0.60)))
    if others:
        L += ["", "<b>%s</b>" % ("Also in this window" if groups else "Matches in this window"),
              "<i>\u2705 Pinnacle price found | \u2796 no Pinnacle price yet | \u23F3 not checked yet</i>"]
        used = len("\n".join(L))
        shown = 0
        for m in others:
            chunk = "\n".join(match_line(m))
            if used + len(chunk) + 40 > max_chars:
                L.append("+%d more matches in this window (see their own cards)" % (len(others) - shown))
                break
            L.append(chunk)
            used += len(chunk) + 1
            shown += 1
    return "\n".join(L)


class Blocks:
    def __init__(self, load=None, save=None, send=None, edit=None, now=None):
        self._load, self._save = load, save
        self._send, self._edit = send, edit
        self._now = now or time.time
        self.state = None

    def _st(self) -> dict:
        if self.state is None:
            try:
                blob = self._load() if self._load else None
                self.state = json.loads(blob) if blob else {}
            except Exception:
                self.state = {}
            self.state.setdefault("cards", {})
        return self.state

    def flush(self):
        try:
            if self._save and self.state is not None:
                self._save(json.dumps(self.state))
        except Exception as e:
            log.warning("block cards not saved: %s", e)

    def _tg_send(self, text):
        if self._send:
            return self._send(text)
        from core import telegram_notify as tn
        from config.settings import settings
        res = tn.api("sendMessage", chat_id=settings.TELEGRAM_CHAT_ID, text=text[:4000], parse_mode="HTML",
                     disable_notification=True)
        return ((res or {}).get("result") or {}).get("message_id")

    def _tg_edit(self, msg_id, text):
        if self._edit:
            return self._edit(msg_id, text)
        from core import telegram_notify as tn
        from config.settings import settings
        from services.client import HttpError
        try:
            tn.api("editMessageText", chat_id=settings.TELEGRAM_CHAT_ID, message_id=msg_id, text=text[:4000], parse_mode="HTML")
            return True
        except HttpError as e:
            s = str(e)
            if "not modified" in s:
                return True
            if "not found" in s or "can't be edited" in s:
                return False
            raise

    def _note_ticket(self, st, key, day, groups):
        """Remember the legs of the ticket as the card last showed it, so it can be graded from the legs' results."""
        t = ticket([(m, [p for p, holding in picks if holding]) for m, picks in groups])
        tk = st.setdefault("tickets", {})
        if not t:
            if key in tk and not tk[key].get("res"):
                del tk[key]                      # no ticket on the card any more
            return
        legs = []
        for p, m in t["legs"]:
            pid = ((m.get("picks") or {}).get(p.get("pk")) or {}).get("id")
            legs.append(dict(id=pid, desc=p.get("desc"), home=m["home"], away=m["away"], odds=p["odds"], p=p["p"]))
        old = tk.get(key) or {}
        if old.get("res"):
            return
        tk[key] = dict(day=day, t=old.get("t") or int(self._now()), legs=legs, odds=t["odds"], p=t["p"], res=None)

    def settle(self, result_of) -> int:
        """result_of(pick id) -> 'won' | 'lost' | 'void' | None. A ticket loses as soon as one leg loses and wins when every
        leg (void legs dropped) has won. Returns how many tickets were settled now."""
        st, n = self._st(), 0
        for k, t in (st.get("tickets") or {}).items():
            if t.get("res"):
                continue
            rs = []
            for leg in t["legs"]:
                try:
                    rs.append(result_of(leg.get("id")) if leg.get("id") is not None else None)
                except Exception:
                    rs.append(None)
            if "lost" in rs:
                t["res"] = "lost"
            elif all(r in ("won", "void") for r in rs):
                t["res"] = "void" if all(r == "void" for r in rs) else "won"
            if t.get("res"):
                t["graded_t"] = int(self._now())
                n += 1
        if n:
            self.flush()
        return n

    def ticket_rows(self, day: str | None = None):
        """(n_graded, won, expected win %, return %) of graded tickets (void not counted); day = the block day label filter."""
        g = [t for t in (self._st().get("tickets") or {}).values() if t.get("res") in ("won", "lost")
             and (day is None or t.get("day") == day)]
        if not g:
            return None
        w = sum(1 for t in g if t["res"] == "won")
        ret = sum((t["odds"] - 1) if t["res"] == "won" else -1.0 for t in g)
        return len(g), w, 100.0 * sum(t["p"] for t in g) / len(g), 100.0 * ret / len(g)

    def update(self, matches: dict) -> int:
        """matches: the tracker's state['matches']. Sends / edits one card per block that has a PLAY NOW pick."""
        if not enabled():
            return 0
        st = self._st()
        by_block = {}
        now = self._now()
        lead = _f("BLOCK_LEAD_MIN", 60) * 60
        for m in matches.values():
            if m.get("skipped"):
                continue
            key, label, day = block_of(m["tip"])
            by_block.setdefault(key, (label, day, []))[2].append(m)
        n = 0
        for key, (label, day, ms) in by_block.items():
            ms.sort(key=lambda m: m["tip"])
            has_play = any(m.get("play_keys") for m in ms)
            try:
                started_or_close = now >= block_start_epoch(key) - lead
            except Exception:
                started_or_close = True
            if not has_play and not started_or_close and key not in st["cards"]:
                continue                       # a quiet window gets its card one hour before it starts
            groups = [(m, active_picks(m)) for m in ms if m.get("play_keys")]
            others = [m for m in ms if not m.get("play_keys")]
            text = render(label, day, groups, others)
            try:
                self._note_ticket(st, key, day, groups)
            except Exception as e:
                log.warning("ticket not recorded: %s", e)
            h = hashlib.md5(text.encode("utf-8")).hexdigest()
            card = st["cards"].get(key)
            try:
                if card is None:
                    mid = self._tg_send(text)
                    st["cards"][key] = dict(msg_id=mid, h=h, t=int(self._now()))
                    n += 1
                elif card.get("h") != h:
                    if not self._tg_edit(card["msg_id"], text):
                        card["msg_id"] = self._tg_send(text)
                    card["h"] = h
                    n += 1
            except Exception as e:
                log.warning("block card %s failed: %s", key, e)
        cutoff = self._now() - 3 * 86400
        for k in [k for k, v in st["cards"].items() if v.get("t", 0) < cutoff]:
            del st["cards"][k]
        for k in [k for k, v in (st.get("tickets") or {}).items() if v.get("t", 0) < self._now() - 30 * 86400]:
            del st["tickets"][k]
        self.flush()
        return n
