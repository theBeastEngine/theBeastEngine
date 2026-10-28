"""Manual-approval Telegram bot (long polling, stdlib only). You place bets yourself; no auto-login."""
import csv
import json
import html
import io
import time
from config.leagues import LEAGUES, LEAGUE_KEYS
from config.settings import settings, PINNACLE, GOLD_EXPECTED_ROWS, MIN_EV, TOP_N, SCAN_SECONDS
from core import telegram_notify as tn
from core import cards
from core.subscription import is_authorized
from services.logger import get_logger
from storage import repository as repo
from utils.dates import parse_range

log = get_logger("bot")
HELP = ("/beast prematch scan  /live live scan  /sim simulation scan\n"
        "/odds best edges now  /today all picks today  /league <code|n> <dates>  /match <team|league> best markets of a coming game\n"
        "/bets placed bets  /took <id> <odds>  /bankroll [amount]\n"
        "/result <id> won|lost|void  /score <id> 28-24 30-22 25-28 29-30 [ot=5-3] settles the whole game\n"
        "/export [n] picks as CSV\n"
        "/calib [prematch|live|sim] predicted vs actual win rate\n"
        "/ev summary  /pinnacle gold status  /status  /scan scan summary  /quarters live quarter outlook  /livepin live Pinnacle check\n"
        "/stance locked live stances  /positions open pre-match bets  /clv closing-line check\n"
        "/linkall auto-link missing leagues  /tournaments <name> find a tid  /probe <tid> check one  /addleague  /removeleague <code>  /leagues  /stop  /start")
# The "/" menu Telegram shows in the chat (same list BotFather's /setcommands holds). Registered at start-up so it can
# never drift from the code: every name here must be a cmd_<name> method (a test checks it).
COMMANDS = [
    ("beast", "Run a pre-match scan now"),
    ("live", "Run a live scan now"),
    ("sim", "Run the simulation scan"),
    ("scan", "Summary of the latest scans"),
    ("odds", "Best open edges right now"),
    ("today", "All picks sent today"),
    ("match", "Best markets of a coming game: /match <team or league>"),
    ("league", "Picks of one league: /league nba 3rd to 17th October"),
    ("took", "Log a bet you placed: /took <id> <odds>"),
    ("result", "Settle a bet: /result <id> won|lost|void"),
    ("score", "Settle a whole game from its scores: /score <id> 28-24 30-22 25-28 29-30"),
    ("bets", "Bets you have placed"),
    ("bankroll", "Show or set your bankroll"),
    ("ev", "Expected-value summary"),
    ("export", "Send your picks as CSV"),
    ("calib", "Predicted vs actual win rate of your settled bets"),
    ("winrate", "Real win rate of EVERY graded pick (auto-graded from final scores)"),
    ("shadow", "Paper picks: what the engine would have bet, graded silently"),
    ("qcheck", "Quarter forecasts and suggested tickets, graded against real results"),
    ("keys", "Odds API keys: credits left and which are resting; OddsPapi calls today"),
    ("stance", "Live games with a locked stance"),
    ("positions", "Open pre-match bets and their status"),
    ("clv", "Closing-line check"),
    ("quarters", "Live quarter and half outlook"),
    ("livepin", "Check whether Pinnacle has live prices for games in play"),
    ("scout", "Scout leagues that could be added: Pinnacle price AND SportyBet games"),
    ("papiprobe", "Probe OddsPapi v5: in-play Pinnacle prices and settlement for grading"),
    ("leagues", "All leagues and their Pinnacle source"),
    ("tournaments", "Find an OddsPapi tournament id by name"),
    ("linkall", "Find and verify Pinnacle sources for leagues that lack one"),
    ("probe", "Check an OddsPapi tournament id"),
    ("addleague", "Add a league without a code push"),
    ("removeleague", "Remove a league you added"),
    ("pinnacle", "Pinnacle reference status"),
    ("status", "Engine health"),
    ("stop", "Pause all scanning"),
    ("start", "Resume scanning and show the help"),
]


class Html(str):
    """A command reply that is already Telegram HTML (not escaped before sending)."""


MAX_TOOK_ODDS = 100.0
EXPORT_CHUNK = 3500       # characters per Telegram message (limit is 4096), always cut on a line boundary
EXPORT_COLS = ["id", "time_utc", "mode", "league", "game", "bet", "book", "book_odds", "took_odds", "fair_odds",
               "min_odds", "ev_pct", "stake", "status", "settled_utc"]


def _stamp(v):
    try:
        return v.strftime("%Y-%m-%d %H:%M") if v else ""
    except Exception:
        return str(v or "")


def export_chunks(picks: list) -> list:
    """Picks (any order) -> CSV text cut into Telegram-sized pieces; every piece starts with the header line."""
    def row(p):
        try:
            bet = tn.describe(p)
        except Exception:
            bet = f"{p.get('selection')} {p.get('line') or ''}".strip()
        return [p["id"], _stamp(p.get("created_at")), p.get("mode"), p.get("league"),
                f"{p.get('home')} vs {p.get('away')}", bet, p.get("book"), p.get("book_odds"), p.get("took_odds"),
                round(p["fair_odds"], 3) if p.get("fair_odds") else "", p.get("min_odds"),
                round(p["ev"] * 100, 1) if p.get("ev") is not None else "", p.get("stake"), p.get("status"),
                _stamp(p.get("settled_at"))]

    def line(vals):
        b = io.StringIO()
        csv.writer(b, lineterminator="").writerow(["" if v is None else v for v in vals])
        return b.getvalue()

    head = line(EXPORT_COLS)
    lines = [line(row(p)) for p in sorted(picks, key=lambda p: p["id"])]
    chunks, cur = [], head
    for ln in lines:
        if len(cur) + len(ln) + 1 > EXPORT_CHUNK and cur != head:
            chunks.append(cur)
            cur = head
        cur += "\n" + ln
    if cur != head:
        chunks.append(cur)
    return chunks


def _line(p):
    # CLAUDE FIX: team names are escaped (a name with & or < broke Telegram's HTML parser and the reply was lost)
    return (f"#{p['id']} {p['league'].upper()} {html.escape(tn.describe(p))} @{p['book_odds']:.2f} "
            f"EV {p['ev']*100:+.1f}% [{p['status']}]")


def _profit(pick: dict, res: str) -> float:
    """What a settled bet paid: same rule as repo.settle_pick (price actually taken)."""
    stake = pick.get("stake") or 0.0
    odds = pick.get("took_odds") or pick.get("book_odds") or pick.get("min_odds") or 1.0
    return stake * (odds - 1) if res == "won" else (-stake if res == "lost" else 0.0)


def _money(x: float) -> str:
    return ("+" if x > 0 else "-" if x < 0 else "") + "\u20a6" + format(int(round(abs(x))), ",")


def _settled_line(pick: dict, res: str) -> str:
    try:
        what = f"{tn._mlabel(pick)} {tn.describe(pick)}"
    except Exception:
        what = f"{pick.get('market')} {pick.get('selection')}"
    return f"#{pick['id']} {what} \u2192 {res.upper()}  {_money(_profit(pick, res))}"


class TelegramBot:
    def __init__(self, beast):
        self.beast = beast
        self._running = False

    # ---------- commands (return PLAIN text; escaped once, at send time) ----------
    def handle_text(self, text: str) -> str:
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
            return f"Error: {e}"

    @staticmethod
    def _storage_name():
        try:
            from storage import db
            return db.backend_name()
        except Exception:
            return "unknown"

    def _pinref_line(self):
        pr = getattr(self.beast, "pinref", None)
        if pr is None:
            return ""
        try:
            parts = [f"{lg} {len(b['events'])} games, {pr._age_min(b):.0f} min old"
                     for lg, b in pr.data["leagues"].items()]
            return f"\nPinnacle ref: {'; '.join(parts) or 'empty'} | credits today {pr.spent_today()}/{pr.daily_cap}"
        except Exception:
            return ""

    def cmd_start(self, arg):
        from datetime import datetime
        self.beast.paused = False
        self.beast.breaker_reset_at = datetime.utcnow()   # clears a tripped circuit breaker
        self.beast._breaker_told = False
        return Html(cards.card("\u25B6\uFE0F", "SCANNING ON", "The engine is scanning again.",
                               rows=[("Pre-Match", "every 30 min"), ("Live", "every %d s" % SCAN_SECONDS), ("Sim", "with each scan")],
                               sections=[("Commands", [cards.Raw("<pre>%s</pre>" % html.escape(HELP))])]))

    def cmd_stop(self, arg):
        self.beast.paused = True
        return Html(cards.card("\u23F8\uFE0F", "SCANNING PAUSED", "No scans and no alerts until you resume.",
                               note="Send /start to resume."))

    def _run(self, fn, label):
        picks = fn()
        if picks:
            return Html(cards.card("\U0001F50D", label.upper() + " SCAN", "The scan you asked for has finished.",
                                   rows=[("Sent", "%d pick(s)" % len(picks))],
                                   note="Each pick arrived as its own alert."))
        return Html(cards.card("\U0001F50D", label.upper() + " SCAN", "The scan you asked for has finished.",
                               rows=[("Sent", "0")],
                               note="No market passed the rules right now. /scan shows what came closest."))

    def _pick_list(self, emoji, title, purpose, picks, empty):
        if not picks:
            return Html(cards.card(emoji, title, purpose, note=empty))
        lines = []
        for p in picks:
            lines += cards.pick_block(p, tn.describe, tn._mlabel) + [""]
        return Html(cards.card(emoji, title, purpose, sections=[("%d pick(s)" % len(picks), lines[:-1])]))

    def cmd_beast(self, arg):
        return self._run(self.beast.run_prematch, "Prematch")

    def cmd_live(self, arg):
        return self._run(lambda: self.beast.run_live(top_n=1), "Live")

    def cmd_sim(self, arg):
        return self._run(self.beast.run_sim, "Simulation")

    def cmd_odds(self, arg):
        rows = sorted((p for p in repo.picks_today() if p["status"] == "new"), key=lambda p: p["ev"], reverse=True)[:5]
        return self._pick_list("\U0001F4C8", "BEST OPEN EDGES TODAY", "Picks sent today that you have not acted on, best edge first.",
                               rows, "No open edges today.")

    def cmd_today(self, arg):
        rows = repo.picks_today()[:25]
        return self._pick_list("\U0001F4C5", "PICKS TODAY", "Every pick sent today, newest first.", rows, "No picks today.")

    def cmd_league(self, arg):
        bits = arg.split(None, 1)
        if not bits:
            return "Usage: /league nba 3rd to 17th October 2026\nCodes: " + ", ".join(LEAGUE_KEYS)
        code = bits[0].lower()
        if code.isdigit() and 1 <= int(code) <= len(LEAGUE_KEYS):
            code = LEAGUE_KEYS[int(code) - 1]
        if code not in LEAGUES:
            return "Unknown league. Codes: " + ", ".join(LEAGUE_KEYS)
        start, end = parse_range(bits[1] if len(bits) > 1 else "")
        rows = repo.picks_between(code, start, end)[:30]
        name = LEAGUES[code].get("name", code.upper())
        head = f"{name} {start:%d %b}\u2013{(end.fromordinal(end.toordinal() - 1)):%d %b %Y}"
        return self._pick_list("\U0001F3C6", "LEAGUE PICKS", head, rows, "No picks in that range.")

    def cmd_export(self, arg):
        try:
            n = int(arg.strip()) if arg.strip() else 200
        except ValueError:
            return "Usage: /export [how many, default 200, max 1000]"
        n = max(1, min(n, 1000))
        picks = repo.all_picks(n)
        if not picks:
            return "No picks to export yet."
        chunks = export_chunks(picks)
        failed = 0
        for c in chunks:
            if not tn.send("<pre>" + html.escape(c) + "</pre>"):
                failed += 1
        if self._storage_name() == "postgres":
            keep = "Your picks are kept in the database, so a deploy does not erase them. Keep this copy for your records."
        else:
            keep = "Copy them somewhere safe BEFORE any deploy: the Free plan wipes this data."
        return Html(cards.card("\U0001F4E4", "EXPORT", "Your picks as a CSV table. Copy it, or send it to me to check results.",
                               rows=[("Exported", f"{len(picks)} picks"), ("Messages", str(len(chunks)))],
                               sections=[("", ([f"{failed} message(s) failed to send."] if failed else []))],
                               note=f"Exported {len(picks)} picks in {len(chunks)} message(s). {keep}"))

    def cmd_match(self, arg):
        """Best markets of the coming games that match a team or league name (nothing is sent as an alert or saved)."""
        return Html(self.beast.match_check(arg))

    def cmd_livepin(self, arg):
        """Does OddsPapi carry LIVE Pinnacle prices for the games in play, or are they frozen at tip-off? (costs 1-3 calls)"""
        try:
            return self.beast.livepin_check(arg)
        except RuntimeError as e:
            return "Live Pinnacle check: %s" % e

    def cmd_scout(self, arg):
        """Which candidate leagues have BOTH Pinnacle prices (OddsPapi) and SportyBet games? Read-only, ~10-20 OddsPapi calls."""
        from core import scout
        pr = getattr(self.beast, "pinref", None)
        if pr is None:
            return "Pinnacle reference is not running."
        return scout.run(pr, arg)

    def cmd_papiprobe(self, arg):
        """Two read-only probes of OddsPapi v5 (about 5-7 calls): live Pinnacle prices and settlement results."""
        from core import papiprobe
        return Html(papiprobe.run(self.beast, arg))

    def cmd_calib(self, arg):
        """Calibration of settled bets: predicted vs actual win rate by EV bucket, ROI, closing-line value."""
        from datetime import timedelta
        from core import calib, pinhist
        mode = arg.strip().lower()
        if mode and mode not in ("prematch", "live", "sim"):
            return "Usage: /calib [prematch|live|sim]"
        picks = repo.settled_picks(mode or None)

        def closing(p):
            if p.get("mode") != "prematch" or not p.get("commence"):
                return None
            rows = repo.pin_history_between(p["league"], p["commence"] - timedelta(days=1), p["commence"] + timedelta(days=1))
            return pinhist.closing_prob(p, rows)

        rows = calib.rows_from_picks(picks, closing)
        return Html(cards.card("\U0001F4CA", "CALIBRATION", "Does the predicted win rate match what actually happened? (settled bets only)",
                               rows=[("Mode", mode or "all")],
                               sections=[("", calib.format_report(calib.calibration(rows)).splitlines())]))

    def cmd_result(self, arg):
        usage = "Usage: /result <id> won|lost|void  e.g. /result 12 won"
        bits = arg.split()
        if len(bits) != 2:
            return usage
        try:
            pid = int(bits[0].lstrip("#"))
        except ValueError:
            return usage
        res = bits[1].lower()
        if res not in ("won", "lost", "void"):
            return usage
        pick = repo.get_pick(pid)
        if not pick:
            return f"No pick #{pid}."
        if pick["status"] in ("won", "lost", "void"):
            return f"#{pid} is already {pick['status']} (settled picks are final)."
        if pick["status"] != "placed":
            return f"#{pid} is {pick['status']}, not placed. Log what you took first: /took {pid} <odds>"
        new = repo.settle_pick(pid, res)
        return f"\u2705 BET SETTLED\n{_settled_line(pick, res)}\nBankroll \u20a6{new:,.0f}"

    def cmd_score(self, arg):
        usage = ("Usage: /score <pick id> <score per period, home-away> [ot=home-away]\n"
                 "e.g. /score 12 28-24 30-22 25-28 29-30   (add ot=5-3 if it went to overtime)\n"
                 "Settles EVERY placed bet of that game at once.")
        bits = arg.split()
        if len(bits) < 3:
            return usage
        try:
            pid = int(bits[0].lstrip("#"))
        except ValueError:
            return usage
        pick = repo.get_pick(pid)
        if not pick:
            return f"No pick #{pid}."
        from core import settle as st
        from core.live_model import fmt
        try:
            periods, ot = st.parse_scores(bits[1:], fmt(pick["league"])[0])
        except ValueError as e:
            return str(e)
        games = repo.placed_picks_for_event(pick["league"], pick["event_id"])
        if not games:
            return f"#{pid}: no placed bets on this game (nothing to settle). Log bets with /took first."
        lines, unsure, total = [], [], 0.0
        for p in games:
            res = st.settle(p, periods, ot)
            if res is None:
                unsure.append(p["id"])
                continue
            new = repo.settle_pick(p["id"], res)
            lines.append(_settled_line(p, res))
            total += _profit(p, res)
        out = "\u2705 BET SETTLED\n" + "\n".join(lines) if lines else "Nothing settled."
        if lines:
            out += f"\nGame total {_money(total)}  \u00B7  Bankroll \u20a6{new:,.0f}"
        if unsure:
            out += "\nNot sure about " + ", ".join(f"#{i}" for i in unsure) + " (split line / team total): use /result."
        return out

    def cmd_bets(self, arg):
        return self._pick_list("\U0001F4B5", "YOUR PLACED BETS", "The last bets you marked as placed.",
                               repo.placed_bets(15), "No placed bets yet. Tap a number under an alert, or use /took <id> <odds>.")

    def cmd_took(self, arg):
        try:
            pid, odds = arg.split()
            pid, odds = int(pid.lstrip("#")), float(odds)
        except ValueError:
            return "Usage: /took <id> <odds>  e.g. /took 12 2.05"
        pick = repo.get_pick(pid)
        if not pick:
            return f"No pick #{pid}."
        if odds <= 1.0 or odds > MAX_TOOK_ODDS:
            return f"Odds must be between 1.01 and {MAX_TOOK_ODDS:g}."
        repo.set_took_odds(pid, odds)
        if pick["status"] in ("new", "skipped"):  # CLAUDE FIX: logging the odds you took means you placed it
            repo.set_status(pid, "placed")
        note = None
        if pick.get("min_odds") and odds < pick["min_odds"]:
            note = f"Below the minimum {pick['min_odds']:.2f}: the edge is gone at that price."
        return Html(cards.card("\u2705", "BET LOGGED", "Saved as placed at the price you took.",
                               rows=[("Pick", f"#{pid}"), ("Odds taken", f"{odds:.2f}"),
                                     ("Minimum odds", f"{pick['min_odds']:.2f}" if pick.get("min_odds") else "n/a")],
                               note=note))

    def cmd_bankroll(self, arg):
        if arg.strip():
            try:
                amt = float(arg.replace(",", "").replace("\u20a6", "").strip())
            except ValueError:
                return "Usage: /bankroll 250000"
            if amt <= 0:
                return "Bankroll must be above 0."
            repo.bankroll_set(amt, "manual")
        return Html(cards.card("\U0001F4B0", "BANKROLL", "The money your stakes are sized from (5% cap per bet).",
                               rows=[("Bankroll", cards.money(repo.bankroll_current()))],
                               note="Change it with /bankroll <amount>." if not arg.strip() else "Updated."))

    def cmd_ev(self, arg):
        rows = repo.picks_today()
        if not rows:
            return Html(cards.card("\U0001F4CA", "TODAY'S EDGE", "How good today's picks look on paper.",
                                   note=f"No picks today. Rules: edge {MIN_EV:.0%} or more, best {TOP_N} markets per match."))
        staked = sum(p["stake"] for p in rows if p["status"] in ("placed", "won", "lost"))
        return Html(cards.card("\U0001F4CA", "TODAY'S EDGE", "How good today's picks look on paper (not results).",
                               rows=[("Picks sent", str(len(rows))),
                                     ("Average edge", "%+.1f%%" % (sum(p["ev"] for p in rows) / len(rows) * 100)),
                                     ("Staked", cards.money(staked))]))

    def cmd_pinnacle(self, arg):
        rows = repo.gold_rows(PINNACLE)
        real = sum(1 for r in rows if not r["is_derived"])
        pr = getattr(self.beast, "pinref", None)
        lines, credits = [], None
        if pr is not None:
            try:
                lines = [f"{lg.upper()}: {len(b['events'])} games \u00B7 {pr._age_min(b):.0f} min old"
                         for lg, b in pr.data["leagues"].items()]
                credits = f"{pr.spent_today()}/{pr.daily_cap}"
            except Exception:
                pass
        return Html(cards.card("\U0001F4E1", "PINNACLE REFERENCE", "Bets are priced from the live Pinnacle price of the same game.",
                               rows=[("Credits today", credits or "?"),
                                     ("Fallback table", f"{len(rows)}/{GOLD_EXPECTED_ROWS} cells ({real} from Pinnacle)")],
                               sections=[("Cached games by league", lines or ["empty"])],
                               note="The fallback table is not used to price bets."))

    def cmd_scan(self, arg):
        """The same summary the engine sends every hour, on demand (does not reset the hourly window)."""
        return Html(self.beast.build_digest(reset=False))

    def cmd_stance(self, arg):
        """Locked live stances (one per game: winner side + over/under)."""
        rows = self.beast.stances.board()
        if not rows:
            return Html(cards.card("\U0001F512", "LIVE STANCES", "The side the engine has locked for a live game.",
                                   note="No locked live stance right now."))
        return Html(cards.card("\U0001F512", "LIVE STANCES", "The side the engine has locked for a live game. It only changes on a strict reversal.",
                               sections=[("", [cards.Raw("\u2022 %s \u00B7 %s vs %s \u2192 <b>%s</b>" % (
                                   html.escape(str(lg).upper()), html.escape(str(h)), html.escape(str(a)), html.escape(txt)))
                                   for lg, h, a, txt, _t in rows[:15]])]))

    def cmd_positions(self, arg):
        """Open pre-match positions and their state."""
        act = self.beast.positions.active()
        if not act:
            return Html(cards.card("\U0001F4CC", "OPEN PRE-MATCH BETS", "Bets you placed that the engine keeps watching until kick-off.",
                                   note="No open pre-match position."))
        icon = {"hold": "\u2705", "watch": "\u26A0\uFE0F", "exit": "\U0001F6A8", "switch": "\U0001F501"}
        return Html(cards.card("\U0001F4CC", "OPEN PRE-MATCH BETS", "\u2705 hold \u00B7 \u26A0\uFE0F watch \u00B7 \U0001F6A8 exit \u00B7 \U0001F501 switch",
                               sections=[("", [cards.Raw("%s <b>#%s</b> \u00B7 %s vs %s \u2192 %s %s %s" % (
                                   icon.get(v.get("state"), ""), v.get("id"), html.escape(str(v.get("home"))),
                                   html.escape(str(v.get("away"))), html.escape(str(v.get("market"))),
                                   html.escape(str(v.get("sel"))), "" if v.get("line") is None else v.get("line")))
                                   for v in act[:25]])]))

    def cmd_winrate(self, arg):
        """Win rate of every graded pick (auto-graded from final scores plus the ones you settled by hand)."""
        from core import grader
        try:
            picks = repo.all_picks(3000)
            grades = json.loads(repo.kv_get(grader.KEY) or "{}")
        except Exception:
            picks, grades = [], {}
        rows = grader.winrate_rows(picks, grades)
        try:
            rows = rows + self.beast.shadow.rows()
        except Exception:
            pass
        if not rows:
            return Html(cards.card("\U0001F3AF", "WIN RATE", "How often the picks really win.",
                                   note="Nothing graded yet. Full-game picks are graded automatically after the game: NBA, EuroLeague, NCAAB and NBL "
                                        "from final scores, every other league from the last live score the engine saw (only when the "
                                        "result was certain). Anything else needs /score or /result."))
        lines = ["%s \u00B7 %d graded \u00B7 won %d (%.1f%%) \u00B7 expected %.1f%% \u00B7 return %+.1f%%" % (
            lab, n, w, wp, ep, roi) for lab, n, w, wp, ep, roi in rows]
        return Html(cards.card("\U0001F3AF", "WIN RATE", "How often the picks really win, against what the engine expected.",
                               sections=[("Graded picks", lines)],
                               note="Expected = the win chance the engine believed. Return = profit per 1 unit at the price taken. "
                                    "Graded automatically: full-game markets of nba, euroleague, ncaab, nbl from final scores and every other league from the last live score the engine saw (only when the result was certain), plus bets you settled yourself."))

    def cmd_shadow(self, arg):
        """Paper picks: the best full-game market of every match that did not end in PLAY NOW, graded from final scores."""
        sh = self.beast.shadow
        c = sh.counts()
        rows = [("Recorded", str(c["total"])), ("Graded", str(c["graded"])), ("Waiting", str(c["waiting"]))]
        secs = []
        res = sh.rows()
        if res:
            secs.append(("Results", ["%s \u00B7 %d graded \u00B7 won %d (%.1f%%) \u00B7 expected %.1f%% \u00B7 return %+.1f%%" % r for r in res]))
        recent = []
        from core import telegram_notify as tn
        full = str(arg or "").strip().lower() in ("all", "full", "audit")
        if full:
            try:
                for g, lines in sh.breakdown().items():
                    if lines:
                        secs.append(("By " + g, ["%s \u00B7 %d graded \u00B7 won %d (%.0f%%) \u00B7 expected %.0f%% \u00B7 return %+.0f%%" % r
                                               for r in lines[:8]]))
            except Exception as e:
                log.warning("shadow breakdown failed: %s", e)
        for v in sh.recent(25 if full else 8):
            try:
                what = "%s: %s" % (tn._mlabel(v), tn.describe(v))
            except Exception:
                what = "%s %s" % (v.get("market"), v.get("selection"))
            mark = {"won": "\u2705", "lost": "\u274C", "void": "\u21A9\uFE0F"}.get(v.get("res"), "\u23F3")
            recent.append("%s %s \u00B7 %s vs %s \u00B7 %s @ %.2f \u00B7 %s" % (
                mark, str(v.get("league")).upper(), v.get("home"), v.get("away"), what, v.get("odds") or 0,
                "edge seen" if v.get("kind") == "edge" else "closest"))
        if recent:
            secs.append(("Latest paper picks", recent))
        return Html(cards.card("\U0001F4DD", "PAPER PICKS", "What the engine would have bet if its rules were looser. Not bets: they only show whether the rules are right.",
                               rows=rows, sections=secs,
                               note="One per match, taken at the FINAL check when no PLAY NOW came. Graded automatically: NBA, EuroLeague, NCAAB and NBL from final scores, every other league from the last live score the engine saw when the result was certain."))

    def cmd_qcheck(self, arg):
        """Quarter forecasts (Q1 / Q3 messages) vs the real quarter points, and the suggested tickets vs their legs' results."""
        q = self.beast.qgrade
        c = q.counts()
        rows = [("Forecasts recorded", str(c["recorded"])), ("Fully graded", str(c["graded"]))]
        secs = []
        qr = q.rows()
        if qr:
            secs.append(("Forecast vs real points", ["%s \u00B7 %d graded \u00B7 game scored %+.1f vs forecast on average \u00B7 typical miss %.1f pts" % r
                                                   for r in qr]))
        tr = self.beast.tracker.blocks.ticket_rows() if self.beast.tracker.blocks is not None else None
        if tr:
            secs.append(("Suggested tickets", ["%d graded \u00B7 won %d \u00B7 expected %.0f%% \u00B7 return %+.0f%%" % tr]))
        try:
            cp = self.beast.tracker.cplog.rows(self.beast.result_of_pick)
        except Exception:
            cp = []
        if cp:
            secs.append(("Which checkpoint is best (verdict vs real result)", [
                "%s \u00B7 Play: %d graded, won %d, return %+.0f%% \u00B7 Skip: %d graded, %d of them would have won" % r for r in cp]))
        return Html(cards.card("\U0001F4CA", "QUARTER AND TICKET CHECK",
                               "Are the quarter forecasts accurate, and do the suggested tickets win? Not bets: a check on the forecasts.",
                               rows=rows, sections=secs,
                               note="A forecast helps against a bookmaker line only when its typical miss is small (a quarter total usually swings 6 to 8 points on its own). Q4 and 2nd half are graded only when the engine saw the game end at 0:00."))

    def cmd_keys(self, arg):
        """Every Odds API key as ...last4, credits left (as last reported) and whether it is working or resting."""
        import time as _t
        from config.settings import settings
        from services import odds_api as oa
        keys = list(settings.ODDS_API_KEYS)
        rows_k = oa.book().rows(keys)
        n, ok, dead = oa.book().summary(keys)
        lines = []
        for last4, rem, until in rows_k:
            if until:
                hh = _t.strftime("%a %H:%M", _t.gmtime(until + 3600))
                st = "resting until %s (Lagos)" % hh
            else:
                st = "working"
            lines.append("...%s \u00B7 %s \u00B7 %s" % (last4, "credits left %d" % rem if rem is not None else "no credit count yet", st))
        rows = [("Odds API keys", str(n)), ("Working", str(ok)), ("Resting", str(dead))]
        try:
            pr = self.beast.pinref
            rows.append(("OddsPapi calls today", "%d of %d" % (pr._papi_spent_today(), pr.papi_daily_cap)))
            rows.append(("OddsPapi keys", str(len(getattr(pr._papi, "keys", []) or [])) if getattr(pr, "_papi", None) else "not loaded yet"))
        except Exception:
            pass
        secs = [("Odds API keys, in the order they are used", lines[:40])] if lines else []
        return Html(cards.card("\U0001F511", "KEYS", "A key that runs out of credits rests for 24 hours, goes to the end of the order and is tried again later. Nothing is deleted.",
                               rows=rows, sections=secs,
                               note="Credits left is what the Odds API reported on that key's last call. A key that has not been used yet shows no count."))

    def cmd_clv(self, arg):
        """Closing-line check: did Pinnacle's last fair probability move toward our side after we alerted?"""
        rows = [r for r in self.beast.positions.clv_log if r.get("p0") and r.get("p_last")]
        if not rows:
            return Html(cards.card("\U0001F3AF", "CLOSING-LINE CHECK", "Did Pinnacle move toward our side after we alerted? Above 50% is a good sign.",
                                   note="No closed position yet."))
        moves = [r["p_last"] - r["p0"] for r in rows]
        better = sum(1 for m in moves if m > 0)
        return Html(cards.card("\U0001F3AF", "CLOSING-LINE CHECK", "Did Pinnacle move toward our side after we alerted? Above 50% is a good sign.",
                               rows=[("Games checked", str(len(rows))),
                                     ("Moved our way", "%d (%.0f%%)" % (better, 100.0 * better / len(rows))),
                                     ("Average move", "%+.1f probability points" % (100.0 * sum(moves) / len(moves)))]))

    # ---- leagues added without a code push ----
    def cmd_leagues(self, arg):
        rows = []
        for i, code in enumerate(LEAGUE_KEYS, 1):
            c = LEAGUES.get(code, {})
            src = "Odds API" if c.get("odds_api_key") else ("OddsPapi tid %s" % c.get("oddspapi_tid") if c.get("oddspapi_tid") else "no Pinnacle source")
            rows.append("%d. %s \u00B7 %s \u00B7 %s%s" % (i, code, c.get("name", code), src, " (added)" if c.get("added") else ""))
        return Html(cards.card("\U0001F30D", "LEAGUES", "Every league the engine covers and where its Pinnacle price comes from.",
                               sections=[("%d leagues" % len(rows), rows)]))

    def _probe(self, tid):
        pr = getattr(self.beast, "pinref", None)
        if pr is None:
            raise RuntimeError("Pinnacle reference is not running.")
        return pr.probe(tid)

    def cmd_tournaments(self, arg):
        from core import leagues_dyn
        if not arg.strip():
            return "Usage: /tournaments <words>   e.g. /tournaments australia nbl   or   /tournaments greece"
        pr = getattr(self.beast, "pinref", None)
        if pr is None:
            return "Pinnacle reference is not running."
        try:
            rows = pr.tournaments()
        except Exception as e:
            return "Lookup failed: %s" % e
        return leagues_dyn.find_tournaments(rows, arg)

    def cmd_linkall(self, arg):
        from core import leagues_dyn
        pr = getattr(self.beast, "pinref", None)
        if pr is None:
            return "Pinnacle reference is not running."
        try:
            rows = pr.tournaments()
        except Exception as e:
            return "Lookup failed: %s" % e
        return leagues_dyn.linkall(rows, self._probe, repo.kv_get, repo.kv_set)

    def cmd_probe(self, arg):
        from core import leagues_dyn
        a = arg.strip().lower().replace("tid=", "")
        if not a.isdigit():
            return "Usage: /probe <OddsPapi tournament id>   e.g. /probe 250"
        try:
            raw, events, stats = self._probe(int(a))
        except Exception as e:
            return "Probe failed: %s" % e
        return leagues_dyn.probe_text(int(a), raw, events, stats)

    def cmd_addleague(self, arg):
        from core import leagues_dyn
        try:
            cfg = leagues_dyn.parse(arg)
        except ValueError as e:
            return str(e)
        code = cfg["code"]
        if code in leagues_dyn.BUILTIN:
            return "'%s' is a built-in league and cannot be changed from here." % code
        note = ""
        need = [f for f in ("avg_total", "std_margin", "home_adv") if f not in cfg]
        if need and cfg.get("oddspapi_tid"):
            try:
                _raw, events, _st = self._probe(cfg["oddspapi_tid"])
                est = leagues_dyn.estimate_priors(events)
                for f in need:
                    if f in est:
                        cfg[f] = est[f]
                if est:
                    note = "\nPriors estimated from Pinnacle's current lines: " + ", ".join("%s %s" % (k, v) for k, v in est.items() if k in need)
                elif not events:
                    note = "\nOddsPapi has no priced games for this tid right now (off-season?); default priors used."
            except Exception as e:
                note = "\nCould not probe the tid (%s); default priors used." % e
        leagues_dyn.save_one(repo.kv_get, repo.kv_set, cfg)
        leagues_dyn.apply(code, {k: v for k, v in cfg.items() if k != "code"})
        c = LEAGUES[code]
        return ("Added %s (%s). Pinnacle source: %s. Priors: total %s, margin sd %s, home edge %s. SportyBet match words: %s.%s\n"
                "It is scanned from the next cycle. /removeleague %s undoes it."
                % (code, c["name"], "Odds API" if c.get("odds_api_key") else "OddsPapi tid %s" % c.get("oddspapi_tid"),
                   c["avg_total"], c["std_margin"], c["home_adv"],
                   " + ".join(__import__("core.bookmakers._base", fromlist=["x"]).LEAGUE_RULES[code][0][0]), note, code))

    def cmd_removeleague(self, arg):
        from core import leagues_dyn
        code = arg.strip().lower()
        if not code:
            return "Usage: /removeleague <code>"
        if code in leagues_dyn.BUILTIN:
            return "'%s' is built in and cannot be removed." % code
        if not leagues_dyn.delete_one(repo.kv_get, repo.kv_set, code):
            return "'%s' is not an added league." % code
        leagues_dyn.remove_live(code)
        return "Removed %s. Its open positions stay tracked until kick-off." % code

    def cmd_quarters(self, arg):
        """Forecast for the remaining quarters of every live game (no price, not a bet)."""
        from core.quarters import format_outlook
        items = list(getattr(getattr(self.beast, "live_scanner", None), "outlooks", []) or [])
        return Html("\U0001F4C8 <b>QUARTER OUTLOOK</b>\n" + cards.RULE + "\n" + format_outlook(items, limit=10))

    def cmd_status(self, arg):
        from core import digest
        pr = getattr(self.beast, "pinref", None)
        lines = []
        if pr is not None:
            try:
                lines = [f"{lg.upper()}: {len(b['events'])} games \u00B7 {pr._age_min(b):.0f} min old"
                         for lg, b in pr.data["leagues"].items()]
            except Exception:
                pass
        try:
            ctx = digest.gather_ctx(self.beast)
        except Exception:
            ctx = {}
        rows = [("Engine", "PAUSED" if self.beast.paused else "RUNNING"),
                ("Live scan", f"every {SCAN_SECONDS} s"),
                ("Bankroll", cards.money(repo.bankroll_current())),
                ("Credits today", "Odds API %s \u00B7 OddsPapi %s" % (ctx.get("credits", "?"), ctx.get("papi", "?")))]
        if ctx.get("keys"):
            rows.append(("Keys with credits", ctx["keys"]))
        if ctx.get("papi_month"):
            rows.append(("OddsPapi month", ctx["papi_month"]))
        rows.append(("Storage", self._storage_name()))
        return Html(cards.card("\U0001F4CB", "ENGINE STATUS", "Is the engine healthy right now?", rows=rows,
                               sections=[("Pinnacle data", lines), ("Warnings", ["\u26A0\uFE0F " + w for w in ctx.get("warnings") or []])]))

    # ---------- buttons ----------
    def handle_callback(self, data: str):
        """Returns (toast, new_markup or None)."""
        action, _, sid = data.partition(":")
        if action == "tog":
            return self._toggle(sid)
        if action == "tskip":
            ok = False
            try:
                ok = self.beast.tracker.skip(sid)
            except Exception:
                ok = False
            return ("Match skipped: tracking stopped" if ok else "Match not found"), None
        try:
            pid = int(sid)
        except ValueError:
            return "Bad button", None
        pick = repo.get_pick(pid)
        if not pick:
            return "Pick not found", None
        status = pick["status"]
        if action == "place":
            return f"Place at \u2265 {pick['min_odds']:.2f} on your book, then tap PLACED", tn.keyboard(pid, "placing")
        if action == "placed":
            if status in ("won", "lost"):
                return "Already settled", {"inline_keyboard": []}
            repo.set_status(pid, "placed")  # CLAUDE FIX: status is always set, not only when took_odds already existed
            if not pick["took_odds"]:
                repo.set_took_odds(pid, pick["book_odds"])
            return "Logged as placed (use /took to set your real odds)", {"inline_keyboard": []}
        if action == "skip":
            if status in ("placed", "won", "lost"):  # CLAUDE FIX: a stray SKIP tap no longer erases a placed bet
                return f"Already {status}; not skipped", {"inline_keyboard": []}
            repo.set_status(pid, "skipped")
            return "Skipped", {"inline_keyboard": []}
        return "?", None

    def _toggle(self, arg: str):
        """Numbered button under a per-game alert: first tap = placed (at the price shown), second tap = not placed."""
        sid, _, csv = arg.partition(":")
        try:
            pid = int(sid)
            ids = [int(x) for x in csv.split(",") if x] or [pid]
        except ValueError:
            return "Bad button", None
        pick = repo.get_pick(pid)
        if not pick:
            return "Pick not found", None
        status = pick["status"]
        if status in ("won", "lost", "void"):
            toast = "Already settled (%s)" % status
        elif status == "placed":
            repo.set_status(pid, "new")
            toast = "Not placed"
        else:
            repo.set_status(pid, "placed")
            if not pick.get("took_odds"):
                repo.set_took_odds(pid, pick["book_odds"])
            toast = "Logged as placed at %.2f (use /took if your price differs)" % (pick.get("took_odds") or pick["book_odds"])
        placed = set()
        for i in ids:
            try:
                q = repo.get_pick(i)
                if q and q["status"] in ("placed", "won", "lost", "void"):
                    placed.add(i)
            except Exception:
                pass
        return toast, tn.ids_keyboard(ids, placed)

    # ---------- polling ----------
    def _authorized(self, chat_id) -> bool:
        return is_authorized(chat_id)

    def process_update(self, u: dict):
        if "message" in u and u["message"].get("text"):
            chat = u["message"]["chat"]["id"]
            text = u["message"]["text"]
            if not settings.TELEGRAM_CHAT_ID:
                tn.send(f"Your chat id is <code>{chat}</code>. Put it in TELEGRAM_CHAT_ID and redeploy.", chat_id=str(chat))
            elif self._authorized(chat):
                # CLAUDE FIX: replies are plain text; escape once here (HELP's <id>/<odds> was rejected as an HTML tag)
                reply = self.handle_text(text)
                tn.send(reply if isinstance(reply, Html) else html.escape(reply), chat_id=str(chat))
        elif "callback_query" in u:
            cq = u["callback_query"]
            chat = cq["message"]["chat"]["id"]
            if not self._authorized(chat):
                return
            toast, markup = self.handle_callback(cq["data"])
            tn.api("answerCallbackQuery", callback_query_id=cq["id"], text=toast)
            if markup is not None:
                tn.api("editMessageReplyMarkup", chat_id=chat, message_id=cq["message"]["message_id"], reply_markup=markup)

    def register_commands(self):
        """Publish COMMANDS as the bot's menu (Telegram's setMyCommands). Never fatal."""
        try:
            tn.api("setMyCommands", commands=[dict(command=c, description=d[:250]) for c, d in COMMANDS])
            log.info("bot command menu registered (%d commands)", len(COMMANDS))
        except Exception as e:
            log.warning("bot command menu not registered: %s", e)

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
