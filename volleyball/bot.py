"""The volleyball Telegram bot (long polling). Owner-only: the chat id in TELEGRAM_CHAT_ID."""
from __future__ import annotations

import html
import logging
import time

from config.settings import settings
from core import cards as ccards
from core import telegram_notify as tn
from core.subscription import is_authorized
from volleyball import cards, cfg
from volleyball.cards import clock
from volleyball import model

log = logging.getLogger("volleyball.bot")
COMMANDS = [
    ("today", "All volleyball picks sent today"),
    ("open", "Open picks and their markets to take"),
    ("agenda", "Volleyball matches in the next 24 hours"),
    ("match", "One match's tracker: /match <team>"),
    ("bets", "Picks you have placed"),
    ("took", "Log your real odds: /took <id> <odds>"),
    ("result", "Settle a pick by hand: /result <id> won|lost|void"),
    ("bankroll", "Show or set your volleyball bankroll: /bankroll [amount]"),
    ("winrate", "Real record of every graded pick"),
    ("checkpoints", "Which check (T-60, T-30, T-15, T-5) is best"),
    ("keys", "Odds API keys and credits used by volleyball today"),
    ("leagues", "Leagues with games now and how many have a Pinnacle price"),
    ("status", "Volleyball engine health"),
    ("stop", "Pause volleyball scanning"),
    ("start", "Resume volleyball scanning"),
]
HELP = "Volleyball commands: " + "  ".join("/%s" % c for c, _ in COMMANDS)


class Html(str):
    """Reply that is already Telegram HTML."""


def _pl(p) -> str:
    return "#%s %s | %s v %s | %s @ %.2f | %s" % (p["id"], html.escape(p["lname"]), html.escape(p["home"]), html.escape(p["away"]),
                                                    html.escape(p["desc"]), p.get("took") or p["odds"], p["status"].upper())


class VolleyballBot:
    def __init__(self, engine):
        self.eng = engine
        self._running = False

    def handle_text(self, text: str):
        parts = text.strip().split(None, 1)
        if not parts:
            return HELP
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1] if len(parts) > 1 else ""
        fn = getattr(self, "cmd_" + cmd.lstrip("/"), None)
        if not fn or not cmd.startswith("/"):
            return HELP
        try:
            return fn(arg)
        except Exception as e:
            log.exception("command %s failed", cmd)
            return "Error: %s" % e

    # ---- commands
    def cmd_start(self, arg):
        self.eng.paused = False
        return HELP

    def cmd_stop(self, arg):
        self.eng.paused = True
        return "Volleyball scanning paused. /start resumes it."

    def cmd_status(self, arg):
        text, _ = self.eng.status_text()
        return Html(text + ("\n<b>PAUSED</b>" if self.eng.paused else ""))

    def cmd_today(self, arg):
        self.eng._load()
        d = cards.local(self.eng._now()).strftime("%Y%m%d")
        ps = [p for p in self.eng.picks["picks"].values() if cards.local(p["created"]).strftime("%Y%m%d") == d]
        return Html("<b>VOLLEYBALL PICKS TODAY</b> (%d)\n" % len(ps) + ("\n".join(_pl(p) for p in sorted(ps, key=lambda p: p["id"])) or "None yet."))

    def cmd_open(self, arg):
        self.eng._load()
        ps = [p for p in self.eng.picks["picks"].values() if p["status"] in ("new", "placed")]
        return Html("<b>OPEN VOLLEYBALL PICKS</b> (%d)\n" % len(ps) + ("\n".join(_pl(p) + " | play at %.2f+ | kick-off %s" % (p["min_odds"], clock(p["tip"]))
                                                                for p in sorted(ps, key=lambda p: p["tip"])) or "None."))

    def cmd_bets(self, arg):
        self.eng._load()
        ps = [p for p in self.eng.picks["picks"].values() if p.get("took")]
        return Html("<b>PLACED VOLLEYBALL BETS</b> (%d)\n" % len(ps) + ("\n".join(_pl(p) for p in sorted(ps, key=lambda p: p["id"])[-25:]) or "None."))

    def cmd_agenda(self, arg):
        self.eng._load()
        now = self.eng._now()
        out = cards.agenda(now, [dict(id=e["id"], lid=e["lid"], lname=e["lname"], home=e["home"], away=e["away"], t=e["t"])
                                 for e in self.eng.feed.upcoming(24 * 3600, now)], self.eng.priced)
        return Html(out[0] + ("\n(%d more part(s) are on the morning card)" % (len(out) - 1) if len(out) > 1 else ""))

    def cmd_match(self, arg):
        self.eng._load()
        q = arg.strip().lower()
        if not q:
            return "Use /match <team name>"
        for m in sorted(self.eng.matches.values(), key=lambda m: m["tip"]):
            if q in m["home"].lower() or q in m["away"].lower():
                return Html(cards.render(m))
        return "No tracked match with that name."

    def cmd_took(self, arg):
        try:
            pid, odds = arg.split()
            p = self.eng.pick(int(pid))
            odds = float(odds)
        except Exception:
            return "Use /took <id> <odds>"
        if not p or not (1.01 <= odds <= 100):
            return "Pick not found or odds not valid."
        p["took"] = odds
        if p["status"] == "new":
            p["status"] = "placed"
        self.eng.flush()
        return "#%s logged as placed at %.2f." % (pid, odds)

    def cmd_result(self, arg):
        try:
            pid, res = arg.split()
            p = self.eng.pick(int(pid))
        except Exception:
            return "Use /result <id> won|lost|void"
        if not p or res not in ("won", "lost", "void"):
            return "Pick not found, or the result is not won / lost / void."
        odds = p.get("took") or p["odds"]
        p["status"], p["ret"] = res, round({"won": odds - 1, "lost": -1.0, "void": 0.0}[res], 4)
        self.eng.flush()
        return "#%s set to %s." % (pid, res)

    def cmd_bankroll(self, arg):
        st = self.eng.stats()
        return "Volleyball bankroll ₦%s (start ₦%s, placed bets only)." % (format(int(round(st["bankroll"])), ","), format(int(cfg.bankroll_start()), ","))

    def cmd_winrate(self, arg):
        st = self.eng.stats()
        rate = (100.0 * st["won"] / st["graded"]) if st["graded"] else 0.0
        rows = [("Picks sent", str(st["total"])), ("Graded", str(st["graded"])), ("Won", "%d (%.0f%%)" % (st["won"], rate)),
                ("Return, 1 unit each", "%+.2f units" % st["flat_units"]), ("Open", str(st["open"]))]
        return Html(ccards.card("\U0001F4CA", "VOLLEYBALL RECORD", "Every pick the engine sent, graded from the real final score (set scores).",
                                rows=rows, note="Under about 30 graded picks this says very little."))

    def cmd_checkpoints(self, arg):
        rows = self.eng.cplog.rows(self.eng.result_of_pick) if self.eng.cplog else []
        lines = ["%s | Play: %d graded, won %d, return %+.0f%% | Skip: %d graded, %d would have won" % r for r in rows]
        return Html(ccards.card("\U0001F4CA", "WHICH CHECKPOINT IS BEST", "Each check's Play / Skip verdict against the real result.",
                                sections=[("", lines or ["No graded picks yet."])]))

    def cmd_keys(self, arg):
        from services import odds_api as oa
        keys = list(settings.ODDS_API_KEYS)
        n, ok, dead = oa.book().summary(keys)
        sp = self.eng.pin.spend
        rows = [("Odds API keys", "%d (%d working, %d resting)" % (n, ok, dead)),
                ("Volleyball Odds API credits today", "%d of %d" % (sp.get("oa", 0), int(cfg.f("VB_OA_DAILY_CAP", 600)))),
                ("Volleyball OddsPapi calls today", "%d of %d" % (sp.get("pp", 0), int(cfg.f("VB_PAPI_DAILY_CAP", 120))))]
        return Html(ccards.card("\U0001F511", "VOLLEYBALL KEYS", rows=rows))

    def cmd_leagues(self, arg):
        now = self.eng._now()
        ev = self.eng.feed.upcoming(cfg.horizon_h() * 3600, now)
        by = {}
        for e in ev:
            d = by.setdefault(e["lname"], [0, 0])
            d[0] += 1
            d[1] += 1 if self.eng.priced(e) else 0
        lines = ["%s: %d/%d" % (k, v[1], v[0]) for k, v in sorted(by.items(), key=lambda kv: (-kv[1][1], kv[0]))]
        return Html(ccards.card("⚽", "LEAGUES WITH GAMES NOW", "Games with a Pinnacle price / games, next %d hours." % int(cfg.horizon_h()),
                                sections=[("", lines[:60] or ["No games."]), ("", ["+%d more leagues" % (len(lines) - 60)] if len(lines) > 60 else [])]))

    # ---- polling
    def handle_callback(self, data: str):
        action, _, rest = data.partition(":")
        if action == "ptog":
            sid, _, csv = rest.partition(":")
            toast = self.eng.toggle(int(sid))
            ids = [int(x) for x in csv.split(",") if x]
            btn = []
            for i, pid in enumerate(ids):
                p = self.eng.pick(pid) or {}
                done = p.get("status") in ("placed", "won", "lost", "void", "halfwon", "halflost")
                btn.append({"text": ("✅%d" if done else "%d") % (i + 1), "callback_data": "ptog:%s:%s" % (pid, csv)})
            key = (self.eng.pick(int(sid)) or {}).get("mkey")
            rows = [btn] + ([[{"text": "Skip match", "callback_data": "mskip:%s" % key}]] if key else [])
            return toast, {"inline_keyboard": rows}
        if action == "mskip":
            ok = self.eng.skip_match(rest)
            return ("Match skipped: tracking stopped" if ok else "Match not found"), None
        return "?", None

    def process_update(self, u: dict):
        if "message" in u and u["message"].get("text"):
            chat = u["message"]["chat"]["id"]
            if not settings.TELEGRAM_CHAT_ID:
                tn.send("Your chat id is <code>%s</code>. Put it in TELEGRAM_CHAT_ID and redeploy." % chat, chat_id=str(chat))
            elif is_authorized(chat):
                reply = self.handle_text(u["message"]["text"])
                tn.send(reply if isinstance(reply, Html) else html.escape(reply), chat_id=str(chat))
        elif "callback_query" in u:
            cq = u["callback_query"]
            if not is_authorized(cq["message"]["chat"]["id"]):
                return
            toast, markup = self.handle_callback(cq["data"])
            tn.api("answerCallbackQuery", callback_query_id=cq["id"], text=toast)
            if markup is not None:
                tn.api("editMessageReplyMarkup", chat_id=cq["message"]["chat"]["id"], message_id=cq["message"]["message_id"], reply_markup=markup)

    def register_commands(self):
        try:
            tn.api("setMyCommands", commands=[dict(command=c, description=d[:250]) for c, d in COMMANDS])
        except Exception as e:
            log.warning("volleyball command menu not registered: %s", e)

    def run_forever(self):
        self._running = True
        self.register_commands()
        offset = None
        while self._running:
            try:
                res = tn.api("getUpdates", timeout=30, offset=offset, allowed_updates=["message", "callback_query"])
                for u in res.get("result", []):
                    offset = u["update_id"] + 1
                    try:
                        self.process_update(u)
                    except Exception:
                        log.exception("update failed")
            except Exception as e:
                log.warning("poll error: %s", e)
                time.sleep(5)

    def stop(self):
        self._running = False
