"""The handball engine: tracker (one card per priced match, checks at T-3h.. T-60, T-30, T-15, T-5), picks ledger, grading,
agenda / status cards. All state lives in the key-value store (kv_get / kv_set), so a deploy never loses a card or a pick."""
from __future__ import annotations

import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from core import board
from handball import cards, cfg, model
from handball.cards import LATE, local, clock

log = logging.getLogger("handball.engine")
K_MATCH, K_PICK, K_META = "hb_matches", "hb_picks", "hb_meta"
MAX_PICKS = 3
KEEP_S = 14 * 3600


def day_of(t: float) -> str:
    return local(t).strftime("%Y%m%d")


class Engine:
    def __init__(self, kv_get, kv_set, feed, pin, results, send, edit, plain, livecards, cplog=None, now=None):
        self.kv_get, self.kv_set = kv_get, kv_set
        self.feed, self.pin, self.results = feed, pin, results
        self._send, self._edit, self._plain = send, edit, plain
        self.cards = livecards
        self.cplog = cplog
        self._now = now or time.time
        self.matches, self.picks, self.meta = None, None, None
        self.paused = False
        self._last_index = 0.0
        self._last_agenda = 0.0
        self._last_board = 0.0
        self.errors = []

    # ---------------------------------------------------------------- state
    def _load(self):
        if self.matches is not None:
            return
        def rd(k, d):
            try:
                b = self.kv_get(k)
                return json.loads(b) if b else d
            except Exception:
                return d
        self.matches = rd(K_MATCH, {})
        self.picks = rd(K_PICK, {"next": 1, "picks": {}})
        self.meta = rd(K_META, {})

    def flush(self):
        for k, v in ((K_MATCH, self.matches), (K_PICK, self.picks), (K_META, self.meta)):
            try:
                self.kv_set(k, json.dumps(v))
            except Exception as e:
                log.warning("state %s not saved: %s", k, e)

    # ---------------------------------------------------------------- picks
    def _new_pick(self, m: dict, c: dict) -> int:
        pid = self.picks["next"]
        self.picks["next"] = pid + 1
        self.picks["picks"][str(pid)] = dict(
            id=pid, mkey=m["key"], lname=m["lname"], home=m["home"], away=m["away"], tip=m["tip"], mkt=c["mkt"], line=c["line"],
            sel=c["sel"], desc=model.describe(c, m["home"], m["away"]), odds=round(c["odds"], 3), p=round(c["p"], 4),
            fair=round(c["fair_odds"], 3), ev=round(c["ev"], 4), min_odds=round(c["fair_odds"] * (1 + cfg.min_ev()), 2),
            status="new", created=int(self._now()), took=None, stake=cfg.stake(), ret=None, score=None)
        lk = self.pin.link.get(m.get("soft_id") or m["key"]) if hasattr(self.pin, "link") else None
        if lk and str(lk[0]).startswith("oa:"):
            self.picks["picks"][str(pid)].update(oa_key=str(lk[0])[3:], oa_id=str(lk[1]))
        elif lk and str(lk[0]).startswith("pp:") and str(lk[1]).startswith("papi-"):
            self.picks["picks"][str(pid)].update(pp_id=str(lk[1])[5:])         # OddsPapi fixture id: graded from its settlement feed
        return pid

    def _pp_result(self, p: dict):
        """Final 90-minute score of a pick whose Pinnacle game came from OddsPapi: one settlement request, at most every 20 minutes
        per pick. None while the game is not over or the reply is not understood (the pick then waits for /result)."""
        fid, papi = p.get("pp_id"), getattr(self.pin, "papi", None)
        if not fid or papi is None:
            return None
        now = self._now()
        if now - p.get("pp_try", 0) < 1200:
            return None
        p["pp_try"] = now
        try:
            data = papi.settlement(fid)
            self.pin._spent("pp", 1)
        except Exception as e:
            log.warning("OddsPapi settlement %s failed: %s", fid, str(e)[:120])
            return None
        from services import oddspapi as op
        sm = op.settlement_summary(data)
        if not sm:
            log.info("OddsPapi settlement %s: not final yet or not understood (keys %s)", fid, sorted(data)[:8] if isinstance(data, dict) else type(data).__name__)
            return None
        if sm["state"] == "void":
            return dict(state="void")
        row = sm["rows"].get("fulltime") or sm["rows"].get("result")
        return dict(state="finished", hg=row[0], ag=row[1]) if row else None

    def pick(self, pid):
        self._load()
        return self.picks["picks"].get(str(pid))

    def result_of_pick(self, pid):
        p = self.pick(pid)
        s = (p or {}).get("status")
        return {"won": "won", "halfwon": "won", "lost": "lost", "halflost": "lost", "void": "void"}.get(s)

    # ---------------------------------------------------------------- tick
    def tick(self):
        self._load()
        if self.paused:
            return
        now = self._now()
        try:
            self.feed.refresh()
        except Exception as e:
            self._err("SportyBet handball feed failed: %s" % e)
        horizon = cfg.horizon_h() * 3600
        events = self.feed.upcoming(horizon, now)
        if now - self._last_index > 20 * 60:
            self._last_index = now
            try:
                self.pin.refresh_index(events, horizon)
            except Exception as e:
                self._err("Pinnacle game index failed: %s" % e)
            try:
                if getattr(self.pin, "papi", None) is not None:
                    self.pin.papi_link([x for x in events if x["id"] not in self.pin.link])
            except Exception as e:
                self._err("OddsPapi link failed: %s" % e)
        self._discover(events, now)
        due = [m for m in self.matches.values() if not m.get("skipped") and m["tip"] > now - 600
               and any(not s["done"] and s["t"] <= now for s in m["slots"])]
        if any(s["kind"] in LATE and not s["done"] and s["t"] <= now for m in due for s in m["slots"]):
            try:
                self.feed.refresh(today_only=True)
            except Exception as e:
                self._err("SportyBet today sweep failed: %s" % e)
        for m in sorted(due, key=lambda m: m["tip"]):
            try:
                self._check(m, now)
            except Exception as e:
                log.exception("check failed for %s v %s", m.get("home"), m.get("away"))
                self._err("check failed for %s v %s: %s" % (m.get("home"), m.get("away"), e))
        for k in [k for k, v in self.matches.items() if v["tip"] < now - KEEP_S]:
            del self.matches[k]
        try:
            self.board(now)
        except Exception as e:
            self._err("board failed: %s" % e)
        self.flush()

    def _err(self, text):
        log.warning(text)
        self.errors = (self.errors + [(self._now(), text)])[-20:]

    def _discover(self, events, now):
        for ev in events:
            if ev["id"] in self.matches or ev["id"] not in self.pin.link or ev["t"] <= now:
                continue
            if ev["t"] - now > cfg.f("HB_TRACKER_H", 8) * 3600:
                continue                                  # the tracker starts 8 hours before the match
            self.matches[ev["id"]] = dict(key=ev["id"], soft_id=ev["id"], lid=ev["lid"], lname=ev["lname"], home=ev["home"],
                                          away=ev["away"], tip=ev["t"], checks=[], play_keys=[], picks={},
                                          slots=cards.plan_slots(now, ev["t"]), msg_id=None, skipped=False, pinged=False)

    # ---------------------------------------------------------------- one check
    def _check(self, m: dict, now: float):
        due = [s for s in m["slots"] if not s["done"] and s["t"] <= now]
        if not m["play_keys"] and not cfg.on("HB_LAST_LOOK_ALWAYS", "0"):
            for s in due:
                if s["kind"] == "last":
                    s["done"] = True                      # T-5 is only for a match that has a live bet signal
            due = [s for s in due if not s["done"]]
            if not due:
                return
        slot = due[-1]
        for s in due:
            s["done"] = True
        late = slot["kind"] in LATE
        soft = self.feed.events.get(m["soft_id"])
        nxt = [s["t"] for s in m["slots"] if not s["done"]]
        prev = m["checks"][-1] if m["checks"] else None
        chk = dict(kind=slot["kind"], label=slot["label"], t=now, status="watch", rows=[], keys=[], pmap={}, away=[], change=[],
                   held=None, stale=None, age=None, why=[], hint="", nxt=nxt[0] if nxt else None, soft_1x2=None, pin_1x2=None)
        if soft is None:
            chk.update(status="hold", held="SportyBet is not showing this game right now")
            return self._finish(m, chk, slot)
        sm = model.complete(soft)
        chk["soft_1x2"] = sm.get("1x2")
        ttl = 30.0 if m["tip"] - now < 3 * 3600 else 60.0
        force = None
        if late and (slot["kind"] in ("t60", "final") or m["play_keys"]):
            force = cfg.f("HB_FORCE_MIN_AGE_MIN", 8.0)
        pev, age, note = self.pin.get(soft, ttl, force)
        chk["age"] = age
        if force is not None and age is not None and age > cfg.confirm_max_age_min():
            chk["stale"] = age
        if pev is None:
            chk.update(status="nopin", why=[note] if note else [])
            return self._finish(m, chk, slot)
        pm = model.complete(pev)
        chk["pin_1x2"] = pm.get("1x2")
        dis = model.agree(sm, pm)
        if dis:
            chk.update(status="hold", held="data check failed: " + dis)
            return self._finish(m, chk, slot)
        cands = model.candidates(sm, pm)
        elig = [c for c in cands if c["why_not"] is None]
        chk["keys"] = [model.pkey(c) for c in elig]
        chk["pmap"] = {model.pkey(c): round(c["p"], 4) for c in cands}
        prev_keys = set((prev or {}).get("keys") or [])
        prev_p = (prev or {}).get("pmap") or {}
        moves = {k: v - prev_p[k] for k, v in chk["pmap"].items() if k in prev_p}
        away_ks = [model.pkey(c) for c in elig if moves.get(model.pkey(c), 0.0) < -cfg.away_pts()]
        chk["away"] = [model.describe(c, m["home"], m["away"]) for c in elig if model.pkey(c) in away_ks]
        consistent = [c for c in elig if model.pkey(c) in prev_keys and model.pkey(c) not in away_ks]
        holding = [c for c in elig if model.pkey(c) in m["play_keys"]]
        status = "watch"
        if consistent or holding:
            status = "play"
        elif elig:
            status = "seen"
        if chk["stale"] is not None and status == "play":
            status = "seen"
        chk["status"] = status
        # which markets are shown / held (one per market family, at most MAX_PICKS)
        if status == "play":
            rows, fam = [], set()
            for c in holding + sorted(consistent, key=lambda c: -c["ev"]):
                if model.pkey(c) in [model.pkey(r) for r in rows]:
                    continue
                f = c["mkt"]
                if f in fam and model.pkey(c) not in m["play_keys"]:
                    continue
                if len(rows) >= MAX_PICKS:
                    break
                rows.append(c)
                fam.add(f)
            chk["rows"] = [dict(r) for r in rows]
            if chk["stale"] is None:
                for c in rows:
                    k = model.pkey(c)
                    if k not in m["play_keys"]:
                        pid = self._new_pick(m, c)
                        m["play_keys"].append(k)
                        m["picks"][k] = dict(id=pid, desc=model.describe(c, m["home"], m["away"]),
                                             min_odds=round(c["fair_odds"] * (1 + cfg.min_ev()), 2))
        else:
            if elig:
                chk["hint"] = model.describe(elig[0], m["home"], m["away"])
            chk["table"] = cards.compare_table(cands, m["home"], m["away"], chk.get("soft_1x2"))
            chk["result"] = cards.plain_result(cands, elig, m["home"], m["away"])
            near = [c for c in cands if c["why_not"] and c["sel"] != "draw"][:3]
            chk["why"] = ["%s: %s" % (model.describe(c, m["home"], m["away"]), c["why_not"]) for c in
                          sorted(near, key=lambda c: -c["ev"])]
            if late and m["play_keys"]:
                chk["rows"] = [dict(c) for c in holding]
        # what moved
        if prev and prev.get("pin_1x2") and chk["pin_1x2"]:
            for sel in ("home", "draw", "away"):
                a, b = prev["pin_1x2"][sel], chk["pin_1x2"][sel]
                if abs(a - b) >= 0.03:
                    chk["change"].append("Pinnacle %s %.2f to %.2f." % (sel, a, b))
        if prev and prev.get("soft_1x2") and chk["soft_1x2"]:
            for sel in ("home", "draw", "away"):
                a, b = prev["soft_1x2"][sel], chk["soft_1x2"][sel]
                if abs(a - b) >= 0.03:
                    chk["change"].append("SportyBet %s %.2f to %.2f." % (sel, a, b))
        return self._finish(m, chk, slot)

    def _finish(self, m, chk, slot):
        m["checks"].append(chk)
        text = cards.render(m)
        kb = self._keyboard(m)
        try:
            if m.get("msg_id"):
                ok = self._edit(m["msg_id"], text, kb)
                if not ok:
                    m["msg_id"] = self._send(text, kb, None, True)
            else:
                m["msg_id"] = self._send(text, kb, None, True)
        except Exception as e:
            log.warning("card not sent for %s v %s: %s", m["home"], m["away"], e)
            return
        ping = None
        if slot["kind"] in LATE and m["play_keys"]:
            ping = cards.verdict(m, chk)
            self._log_checkpoint(m, chk, slot)
            m["pinged"] = True
        elif chk["status"] == "play" and not m.get("pinged"):
            ping = "\U0001F514 <b>PLAY NOW</b> · %s v %s (%s)" % (html.escape(m["home"]), html.escape(m["away"]), html.escape(slot["label"]))
            m["pinged"] = True
        if ping:
            try:
                self._send(ping, None, m.get("msg_id"), False)
            except Exception as e:
                log.warning("ping failed: %s", e)

    def _log_checkpoint(self, m, chk, slot):
        if self.cplog is None or chk.get("stale") is not None:
            return
        now_by = {model.pkey(c): c for c in chk.get("rows", [])}
        for k in m["play_keys"]:
            pid = (m["picks"].get(k) or {}).get("id")
            c = now_by.get(k)
            try:
                self.cplog.record(pid, slot["kind"], "play" if c else "skip", (c or {}).get("odds"))
            except Exception as e:
                log.warning("checkpoint not logged: %s", e)

    def _keyboard(self, m):
        ids = [m["picks"][k]["id"] for k in m["play_keys"] if k in m["picks"]]
        if not ids or m.get("skipped") or self._now() > m["tip"]:
            return {"inline_keyboard": []}
        csv = ",".join(str(i) for i in ids)
        btn = []
        for i, pid in enumerate(ids):
            placed = (self.pick(pid) or {}).get("status") in ("placed", "won", "lost", "void", "halfwon", "halflost")
            btn.append({"text": ("✅%d" if placed else "%d") % (i + 1), "callback_data": "ptog:%s:%s" % (pid, csv)})
        return {"inline_keyboard": [btn, [{"text": "Skip match", "callback_data": "mskip:%s" % m["key"]}]]}

    def toggle(self, pid: int):
        """Numbered button: first tap = placed at the price shown, second tap = not placed."""
        self._load()
        p = self.pick(pid)
        if not p:
            return "Pick not found"
        if p["status"] in ("won", "lost", "void", "halfwon", "halflost"):
            return "Already settled (%s)" % p["status"]
        if p["status"] == "placed":
            p["status"] = "new"
            toast = "Not placed"
        else:
            p["status"] = "placed"
            p["took"] = p.get("took") or p["odds"]
            toast = "Logged as placed at %.2f (use /took if your price differs)" % p["took"]
        self.flush()
        return toast

    def skip_match(self, key: str) -> bool:
        self._load()
        m = self.matches.get(key)
        if not m:
            return False
        m["skipped"] = True
        try:
            if m.get("msg_id"):
                self._edit(m["msg_id"], cards.render(m) + "\n<b>Match skipped: tracking stopped.</b>", {"inline_keyboard": []})
        except Exception:
            pass
        self.flush()
        return True

    # ---------------------------------------------------------------- grading
    def grade(self):
        self._load()
        now = self._now()
        n = 0
        for p in self.picks["picks"].values():
            if p["status"] not in ("new", "placed") or now < p["tip"] + 125 * 60:
                continue
            r = self.results.find_oa(p) if hasattr(self.results, "find_oa") else None
            if not r:
                r = self._pp_result(p)
            if not r:
                r = self.results.find(p["home"], p["away"], p["tip"])
            if not r:
                continue
            if r["state"] == "void":
                st, ret = "void", 0.0
            elif r["state"] == "finished":
                st, ret = model.settle(dict(mkt=p["mkt"], line=p["line"], sel=p["sel"], odds=p.get("took") or p["odds"]), int(r["hg"]), int(r["ag"]))
                p["score"] = "%s-%s" % (r["hg"], r["ag"])
            else:
                continue
            was = p["status"]
            p["status"], p["ret"] = st, round(ret, 4)
            p["settled"] = int(now)
            n += 1
            icon = {"won": "✅ WON", "halfwon": "✅ HALF WON", "lost": "❌ LOST", "halflost": "❌ HALF LOST", "void": "➖ VOID"}[st]
            try:
                self._plain("%s | %s | %s v %s %s%s" % (icon, html.escape(p["desc"]), html.escape(p["home"]), html.escape(p["away"]),
                                                       p.get("score") or "", "" if was == "placed" else " (not placed)"), True)
            except Exception:
                pass
        if n:
            self.flush()
        return n

    # ---------------------------------------------------------------- cards
    def priced(self, g) -> bool:
        return g["id"] in self.pin.link

    def agenda(self, now: float):
        """The day's match list as cards edited all day (new games added, prices ticked, started games marked)."""
        self._load()
        day = day_of(now)
        if now - self._last_agenda < 25 * 60:
            return
        st = self.meta.get("agenda") or {}
        if st.get("day") != day:
            if local(now).hour < int(cfg.f("AGENDA_HOUR", 8)):
                return
            st = dict(day=day, games=[])
        have = {g["id"] for g in st["games"]}
        for ev in self.feed.upcoming(24 * 3600, now):
            if ev["id"] not in have and ev["t"] > now:
                st["games"].append(dict(id=ev["id"], lid=ev["lid"], lname=ev["lname"], home=ev["home"], away=ev["away"], t=ev["t"]))
        self._last_agenda = now
        for i, text in enumerate(cards.agenda(now, st["games"], self.priced)):
            self.cards.put("hb_agenda:%d" % i, text, day)
        self.meta["agenda"] = st

    def _tracker_mark(self, g, now):
        m = self.matches.get(g["id"])
        if g["t"] <= now:
            return "▶"
        if m and m.get("play_keys"):
            return "🟢"
        if m and not m.get("skipped"):
            return "👀"
        return "⏳"

    def _tracker_note(self, g, now):
        m = self.matches.get(g["id"])
        if g["t"] <= now or m:
            return ""
        return " (tracker from %s)" % cards.clock(g["t"] - cfg.f("HB_TRACKER_H", 8) * 3600)

    def board(self, now: float, force: bool = False):
        """Three lists for the 06:00-06:00 day, each on its own card(s), edited through the day. Pages after the first
        reply to the page before so one tap jumps back."""
        self._load()
        if not force and now - self._last_board < cfg.f("HB_BOARD_MIN", 10) * 60:
            return
        self._last_board = now
        day = board.board_day(now)
        lo, hi = board.bracket(now)
        st = self.meta.get("board") or {}
        if st.get("day") != day:
            st = dict(day=day, soft={}, pin={}, pages={})
        for ev in list(self.feed.events.values()):
            if lo <= ev["t"] < hi:
                st["soft"][ev["id"]] = dict(id=ev["id"], lname=ev["lname"], home=ev["home"], away=ev["away"], t=ev["t"])
        rev = {}
        for sid, v in self.pin.link.items():
            try:
                rev[str(v[1])] = sid
            except Exception:
                pass
        for k, ix in list(getattr(self.pin, "index", {}).items()):
            for pid, h, a, t in ix.get("events", []):
                if t and h and a and lo <= t < hi:
                    st["pin"][str(pid)] = dict(id=str(pid), lname=board.pretty_key(k), home=h, away=a, t=t)
        soft = list(st["soft"].values())
        pins = list(st["pin"].values())
        both = [g for g in soft if g["id"] in self.pin.link]
        sets = {
            "sporty": (soft, lambda g, n: "▶" if g["t"] <= n else ("✅" if g["id"] in self.pin.link else "➖"), None),
            "pin": (pins, lambda g, n: "▶" if g["t"] <= n else ("✅" if g["id"] in rev else "➖"), None),
            "both": (both, self._tracker_mark, self._tracker_note),
        }
        # extra lists: Watchlist, Coverage gaps, Bets and results
        watch = []
        for g in both:
            m = self.matches.get(g["id"])
            c = (m["checks"][-1] if m and m.get("checks") else None)
            if m and c and g["t"] > now and (m.get("play_keys") or c.get("status") == "seen" or c.get("why")):
                watch.append(dict(g, note=(c.get("hint") or ((c.get("why") or [""])[0]))))
        gaps = [dict(g, gap="s") for g in soft if g["id"] not in self.pin.link and g["t"] > now]
        gaps += [dict(g, gap="p") for g in pins if g["id"] not in rev and g["t"] > now]
        bets = [dict(lname=p["lname"], home=p["home"], away=p["away"], t=p["tip"], id=str(p["id"]), p=p)
                for p in self.picks["picks"].values() if lo <= p["tip"] < hi]
        icon = {"won": "✅", "halfwon": "✅", "lost": "❌", "halflost": "❌", "void": "➖"}
        sets["watch"] = (watch, lambda g, n: "🟢" if (self.matches.get(g["id"]) or {}).get("play_keys") else "👀",
                         lambda g, n: (" | " + html.escape(g["note"][:90])) if g.get("note") else "")
        sets["gaps"] = (gaps, lambda g, n: "🔶" if g["gap"] == "s" else "🔷", None)
        sets["bets"] = (bets, lambda g, n: icon.get(g["p"]["status"], "⏳"),
                        lambda g, n: " | %s @ %.2f%s" % (html.escape(g["p"]["desc"]), g["p"]["odds"],
                                                       (" | " + html.escape(str(g["p"]["score"]))) if g["p"].get("score") else ""))
        for kind, (games, mark, extra) in sets.items():
            pages = board.render(kind, now, games, mark, extra)
            for i, text in enumerate(pages, 1):
                key = "hb_board:%s:%d" % (kind, i)
                reply = self.cards.get("hb_board:%s:%d" % (kind, i - 1)).get("msg_id") if i > 1 else None
                if reply:
                    self.cards.put(key, text, day, True, None, reply)
                else:
                    self.cards.put(key, text, day)
            for j in range(len(pages) + 1, int(st["pages"].get(kind, 0)) + 1):
                self.cards.put("hb_board:%s:%d" % (kind, j), board.empty_page(kind, now, j), day)
            st["pages"][kind] = len(pages)
        self.meta["board"] = st

    # ---------------------------------------------------------------- numbers
    def stats(self) -> dict:
        self._load()
        ps = list(self.picks["picks"].values())
        done = [p for p in ps if p["status"] in ("won", "lost", "halfwon", "halflost")]
        wins = sum(1 for p in done if p["status"] in ("won", "halfwon"))
        flat = sum(p["ret"] for p in done if p.get("ret") is not None)
        placed = [p for p in ps if p.get("took")]
        pl = sum(p["stake"] * p["ret"] for p in placed if p.get("ret") is not None)
        return dict(total=len(ps), graded=len(done), won=wins, flat_units=flat, placed=len(placed), placed_profit=pl,
                    bankroll=cfg.bankroll_start() + pl, open=sum(1 for p in ps if p["status"] in ("new", "placed")))

    def _pp_cap(self):
        try:
            return self.pin.pp_cap()
        except Exception:
            return cfg.f("HB_PAPI_DAILY_CAP", 120)

    def _quota_line(self):
        papi = getattr(self.pin, "papi", None)
        rows = getattr(papi, "quota_rows", None)
        if rows:
            return "%d keys, %d of %d requests left this month" % (len(rows), sum(l - u for l, u in rows), sum(l for l, u in rows))
        raw = getattr(papi, "quota_raw", None)
        return "monthly figures not read yet" if raw is None else "account reply not understood (fields: %s)" % (", ".join(raw[:20]) if isinstance(raw, list) else raw)

    def status_text(self, now=None) -> tuple:
        """(ENGINE STATUS text, [warnings])."""
        self._load()
        now = now or self._now()
        st = self.stats()
        ev = self.feed.upcoming(cfg.horizon_h() * 3600, now)
        priced = sum(1 for x in ev if self.priced(x))
        leagues = len({x["lid"] for x in ev})
        sp = self.pin.spend
        warn = []
        if self.feed.last.get("error"):
            warn.append("SportyBet handball feed problem: %s" % self.feed.last["error"])
        if not ev:
            warn.append("SportyBet shows no handball games at all")
        elif ev and priced == 0:
            warn.append("No handball game has a Pinnacle price yet")
        stuck = [p for p in self.picks["picks"].values() if p["status"] in ("new", "placed") and now > p["tip"] + 8 * 3600]
        if stuck:
            warn.append("%d pick(s) could not be graded automatically (no result source for their league): set them with /result <id> won|lost|void" % len(stuck))
        rows = [("SportyBet", "%d games in %d leagues (next %dh)" % (len(ev), leagues, int(cfg.horizon_h()))),
                ("Pinnacle price", "%d of %d games" % (priced, len(ev))),
                ("Odds API credits", "%d of %d today" % (sp.get("oa", 0), int(cfg.f("HB_OA_DAILY_CAP", 600)))),
                ("OddsPapi calls", "%d of %d today (month-safe)" % (sp.get("pp", 0), int(self._pp_cap()))),
                ("OddsPapi keys", self._quota_line()),
                ("Picks", "%d total, %d open, %d graded (%d won)" % (st["total"], st["open"], st["graded"], st["won"])),
                ("Bankroll", "₦%s" % format(int(round(st["bankroll"])), ","))]
        from handball import sporty
        seen = sporty.seen_top(8)
        info = ("SportyBet markets seen: " + "; ".join("%s %s x%d" % (i, n or "?", c) for i, n, c in seen)) if seen else \
            "SportyBet markets seen: none yet. Probe: %s" % (self.feed.last.get("probe") or "not run yet")
        from core.cards import dots
        text = "\n".join(["\U0001F4CB <b>HANDBALL ENGINE STATUS</b> · %s" % clock(now), cards.RULE,
                          "<i>Is the handball engine healthy, and what is it watching?</i>", "",
                          "<pre>%s</pre>" % html.escape(dots(rows))] + [""] * bool(warn) + ["⚠️ <b>%s</b>" % html.escape(w) for w in warn]
                         + ["", "<i>%s</i>" % html.escape(info[:900])])
        return text, warn

    def hourly(self):
        """One silent status card edited in place; an audible alert only for a warning not raised today."""
        self._load()
        now = self._now()
        day = day_of(now)
        text, warn = self.status_text(now)
        self.cards.put("hb_status", text, day)
        import re
        norm = lambda w: re.sub(r"\d+", "#", w)
        c = self.cards.get("hb_warned")
        told = set((c.get("data") or {}).get("w") or []) if c and c.get("day") == day else set()
        new = [w for w in warn if norm(w) not in told]
        if new:
            try:
                self._plain("⚠️ <b>HANDBALL ENGINE WARNING</b>\n" + "\n".join(html.escape(w) for w in new), False)
                st = self.cards._st()
                st["hb_warned"] = dict(day=day, msg_id=0, h="", t=now, data=dict(w=sorted(told | {norm(w) for w in new})))
                self.cards._dirty = True
                self.cards.flush()
            except Exception as e:
                log.warning("warning not sent: %s", e)

    def daily_report(self):
        """08:00: yesterday's handball picks in a few lines (once per local day)."""
        self._load()
        now = self._now()
        day = day_of(now)
        if local(now).hour < int(cfg.f("DAY_REPORT_HOUR", 8)) or self.meta.get("report_day") == day:
            return
        y = (local(now) - timedelta(days=1)).strftime("%Y%m%d")
        ps = [p for p in self.picks["picks"].values() if day_of(p["tip"]) == y]
        done = [p for p in ps if p["status"] in ("won", "lost", "halfwon", "halflost")]
        wins = sum(1 for p in done if p["status"] in ("won", "halfwon"))
        flat = sum(p["ret"] for p in done)
        st = self.stats()
        L = ["\U0001F305 <b>HANDBALL DAY REPORT</b> | %s" % y, cards.RULE,
             "Picks: <b>%d</b> | graded <b>%d</b> | won <b>%d</b> | still open <b>%d</b>" % (len(ps), len(done), wins, len(ps) - len(done)),
             "Return on flat 1-unit stakes: <b>%+.2f units</b>" % flat,
             "Overall: %d graded, %d won, %+.2f units | Bankroll ₦%s" % (st["graded"], st["won"], st["flat_units"],
                                                                         format(int(round(st["bankroll"])), ","))]
        if self.cplog is not None:
            try:
                rows = self.cplog.rows(self.result_of_pick)
                if rows:
                    L += ["", "<b>Which checkpoint is best</b>"] + [
                        "%s | Play: %d graded, won %d, return %+.0f%% | Skip: %d graded, %d would have won" % r for r in rows]
            except Exception:
                pass
        try:
            self._plain("\n".join(L), True)
            self.meta["report_day"] = day
            self.flush()
        except Exception as e:
            log.warning("day report not sent: %s", e)
