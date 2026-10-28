import json
import os
from datetime import timedelta

from config.leagues import LEAGUE_KEYS, LEAGUES, pin_source
from config.settings import settings, GOLD_EXPECTED_ROWS, PINNACLE, MC_SIMS
from core.bookmakers import soft_feed, pinnacle
from core.filter import finalize
from core import gates
from core.digest import ScanStats, near_miss, format_digest, gather_ctx
from core.reports import CloseCalls
from core import stance, positions
from core import tracker as _tracker
from core import grader as _grader
from core import shadow as _shadow
from core import finals as _finals
from core import blocks as _blocks
from core import qgrade as _qgrade
from core import livecards as _livecards
from core import qcard as _qcard
from core import cplog as _cplog
from core.live_scanner import LiveScanner
from core.market_selector import candidates_prematch, gold_index
from core.modes import PREMATCH, SIM
from core.pinref import PinRef, _naive, _now
from core import restflag
from core.pinhist import PriceHistory
from core.teamsched import TeamSchedule, KEY as SCHED_KEY
from core.injuries import InjuryBook
from core.steam import SteamTracker, event_key as _steam_key
from core.simulation import candidates_sim
from services.ratings import matchup_type
from services.validator import validate_signal
from services.logger import get_logger
from storage import repository as repo

log = get_logger("beast")


from utils.dates import commence_dt


def days_ahead() -> float:
    """Prematch/sim only look at games starting within this many days (env DAYS_AHEAD, default 3)."""
    try:
        return float(os.environ.get("DAYS_AHEAD", "3"))
    except ValueError:
        return 3.0


def require_real_pin() -> bool:
    """Every mode needs a REAL Pinnacle price of the same game (env REQUIRE_REAL_PIN=0 turns this off)."""
    return os.environ.get("REQUIRE_REAL_PIN", "1").strip() != "0"


def live_enabled() -> bool:
    """The 2-minute live scan is TheBeast's mandate: ON by default (env LIVE_ENABLED=0 is an emergency off switch)."""
    return os.environ.get("LIVE_ENABLED", "1").strip() != "0"


def within_horizon(commence, days=None, now=None) -> bool:
    """True only for a game that has not started and starts within `days` days. Unknown start time = False."""
    c = _naive(commence)
    if c is None:
        return False
    now = now or _now()
    days = days_ahead() if days is None else days
    return now <= c <= now + timedelta(days=days)


def _has_key(lg) -> bool:
    try:
        return pin_source(lg) is not None
    except Exception:
        return False


def _idx(cache: dict, lg: str) -> dict:
    # CLAUDE FIX: `cache.get(lg) or ...` re-queried the DB for every event when the index was empty
    if lg not in cache:
        cache[lg] = gold_index(lg)
    return cache[lg]


class Beast:
    """Orchestrator. notify(pick_dict) is injected by the Telegram layer (group 4)."""

    def __init__(self, notify=None):
        self.notify = notify
        try:   # leagues added from Telegram (/addleague) come back after every restart
            from core import leagues_dyn
            leagues_dyn.load(repo.kv_get)
            leagues_dyn.load_all_links(repo.kv_get)
        except Exception as e:
            log.warning("added leagues not loaded: %s", e)
        self.pinref = PinRef()  # per-game Pinnacle reference (cached, credit-capped)
        self.teamsched = TeamSchedule(load=lambda: repo.kv_get(SCHED_KEY), save=lambda s: repo.kv_set(SCHED_KEY, s))
        self.pinhist = PriceHistory(repo.add_pin_history)
        self.pinref.on_refresh = self._on_pin_refresh
        self.injuries = InjuryBook()      # NBA injury news (ESPN): note on the alert + short hold on fresh news
        self.steam = SteamTracker()       # Pinnacle main lines over time: hold while a game's line is still moving
        self.live_scanner = LiveScanner(pinref=self.pinref)
        self.paused = False  # /stop and /start
        self.stats = ScanStats()  # feeds the hourly scan summary
        # state that must survive a deploy lives in the key/value table (Neon): live stances, open pre-match positions
        self.stances = stance.StanceBook(load=lambda: repo.kv_get(stance.KEY), save=lambda s: repo.kv_set(stance.KEY, s))
        self.positions = positions.Positions(
            load=lambda: repo.kv_get(positions.KEY), save=lambda s: repo.kv_set(positions.KEY, s),
            clv_load=lambda: repo.kv_get(positions.CLV_KEY), clv_save=lambda s: repo.kv_set(positions.CLV_KEY, s))
        self.tracker = _tracker.MatchTracker(self, load=lambda: repo.kv_get(_tracker.KEY), save=lambda v: repo.kv_set(_tracker.KEY, v))
        self.grader = _grader.Grader(self, load=lambda: repo.kv_get(_grader.KEY), save=lambda v: repo.kv_set(_grader.KEY, v))
        self.shadow = _shadow.Shadow(load=lambda: repo.kv_get(_shadow.KEY), save=lambda v: repo.kv_set(_shadow.KEY, v))
        self.grader.shadow = self.shadow
        self.finals = _finals.shared(load=lambda: repo.kv_get(_finals.KEY), save=lambda v: repo.kv_set(_finals.KEY, v))
        self.grader.finals = self.finals
        try:
            from services import odds_api as _oa
            _oa.book().load = lambda: repo.kv_get("odds_key_state")
            _oa.book().save = lambda v: repo.kv_set("odds_key_state", v)
        except Exception as e:
            log.warning("odds key state not wired: %s", e)
        self.qgrade = _qgrade.QGrade(load=lambda: repo.kv_get(_qgrade.KEY), save=lambda v: repo.kv_set(_qgrade.KEY, v),
                                     finals=self.finals)
        self.tracker.shadow = self.shadow
        self.tracker.cplog = _cplog.CPLog(load=lambda: repo.kv_get(_cplog.KEY), save=lambda v: repo.kv_set(_cplog.KEY, v))
        self.tracker.blocks = _blocks.Blocks(load=lambda: repo.kv_get(_blocks.KEY), save=lambda v: repo.kv_set(_blocks.KEY, v))
        self.cards = _livecards.LiveCards(load=lambda: repo.kv_get(_livecards.KEY), save=lambda v: repo.kv_set(_livecards.KEY, v))
        self.closecalls = CloseCalls()      # steady 'close calls' lists per mode (core/reports.py)
        self._ht_scores, self._ht_seen, self._q3_scores, self._q1_scores = {}, {}, {}, {}   # game -> total points at half-time (start of the 2nd half)
        self._quarter_sent = {}   # (game, phase) -> epoch: one quarter-phase message per game per phase
        self._quarter_loaded = False   # half-time scores and sent phases are also kept in the database (a restart must not lose them)
        self._breaker_told = False
        self._seed_cache = {}
        self.breaker_reset_at = None   # /start clears a tripped breaker: only losses settled after it count

    # --- gold ---
    def ensure_gold(self) -> int:
        """0-credit: fill missing Pinnacle rows from priors; restore from backup first if table is empty."""
        if repo.gold_count(PINNACLE) == 0:
            repo.restore_pinnacle()
        pinnacle.derive_fill()
        n = repo.gold_count(PINNACLE)
        if n < GOLD_EXPECTED_ROWS:
            log.warning("gold pinnacle rows %s < %s", n, GOLD_EXPECTED_ROWS)
        return n

    def build_gold(self) -> dict:
        """Paid, manual. Credits ~ 3 per league that has an Odds API key."""
        return pinnacle.build_gold()

    def breaker_tripped(self) -> bool:
        """Circuit breaker: BREAKER_LOSSES settled losses in a row (your /result entries, newest first) pause all alerts
        until /start. BREAKER_LOSSES=0 turns it off. Protects the bankroll while nobody is watching."""
        try:
            n = int(float(os.environ.get("BREAKER_LOSSES", 6)))
        except ValueError:
            n = 6
        if n <= 0:
            return False
        try:
            rows = [r for r in repo.placed_bets(max(n * 3, 30)) if r.get("status") in ("won", "lost")]
        except Exception:
            return False
        rows = [r for r in rows if r.get("settled_at") is not None and
                (getattr(self, "breaker_reset_at", None) is None or r["settled_at"] > self.breaker_reset_at)]
        rows.sort(key=lambda r: r["settled_at"], reverse=True)
        return len(rows) >= n and all(r["status"] == "lost" for r in rows[:n])

    # --- scans ---
    def _persist(self, picks: list) -> list:
        if picks and self.breaker_tripped():
            if not getattr(self, "_breaker_told", False):
                self._breaker_told = True
                log.warning("circuit breaker tripped: alerts paused")
                try:
                    from core import telegram_notify
                    telegram_notify.send("\U0001F6D1 <b>CIRCUIT BREAKER</b>\nYour last settled bets were all losses, so alerts "
                                         "are paused to protect the bankroll. Send /start when you want them back.")
                except Exception as e:
                    log.warning("breaker notice failed: %s", e)
            return []
        # CLAUDE FIX: final gate for EVERY mode: EV must reach MIN_EV (4%), then the same rules as perception
        def _floor(p):
            return (settings.MIN_EV_LIVE if p.get("mode") == "live" else settings.MIN_EV) - 1e-9
        picks = [p for p in picks if (p.get("ev") or 0) >= _floor(p) and validate_signal(p)]
        to_send = []
        for p in picks:
            if p.get("mode") == "live" and p.get("stance_decision") is not None:
                try:   # the stance is locked when the first live alert goes out; a reversal replaces it (labelled)
                    d = p["stance_decision"]
                    self.stances.commit(p, d)
                    gk = stance.game_key(p)
                    p["stance"] = self.stances.stance_text(gk, str(p.get("home") or ""), str(p.get("away") or ""))
                    if d.get("reversal"):
                        p["reversal"] = d.get("reason") or "the game has turned"
                except Exception as e:
                    log.warning("stance commit failed: %s", e)
            if p.get("mode") == "prematch" and not p.get("rest_note"):
                try:   # advisory note only (shown on the alert, never changes the pick)
                    p["rest_note"] = self._rest_note(p)
                except Exception as e:
                    log.warning("rest note skipped: %s", e)
                try:   # advisory too: who is Out / Day-To-Day (NBA only)
                    p["injury_note"] = self.injuries.note(p["league"], p["home"], p["away"])
                except Exception as e:
                    log.warning("injury note skipped: %s", e)
            p["id"] = repo.add_pick(
                mode=p["mode"], league=p["league"], event_id=p["event_id"], home=p["home"], away=p["away"],
                commence=commence_dt(p.get("commence")), market=p["market"], selection=p["selection"], line=p.get("line"),
                fair_odds=p["fair_odds"], min_odds=p["min_odds"], book=p["book"], book_odds=p["odds"],
                ev=p["ev"], stake=p["stake"])
            if self.notify:
                if self._group_alerts():
                    to_send.append(p)    # one message per game, sent below
                else:
                    try:
                        # CLAUDE FIX: only a delivered alert is marked sent (notify returns False when Telegram dropped it;
                        # None from an older notify callback still counts as delivered)
                        if self.notify(p) is not False:
                            repo.mark_sent(p["id"])
                        else:
                            log.warning("pick %s saved but NOT delivered", p["id"])
                    except Exception as e:
                        log.warning("notify failed: %s", e)
            if p.get("mode") == "prematch" and positions.enabled():
                try:
                    self.positions.register(p)
                except Exception as e:
                    log.warning("position register failed: %s", e)
        if to_send:
            self._send_groups(to_send)
        try:
            self.stances.flush()
        except Exception as e:
            log.warning("stance flush failed: %s", e)
        return picks

    def _on_pin_refresh(self, league, events, scope="main"):
        """Called by PinRef after every league refresh: remember who plays when, and log the Pinnacle price history."""
        try:
            self.teamsched.record(league, events, scope)
        except Exception as e:
            log.warning("team schedule not recorded for %s: %s", league, e)
        try:
            self.pinhist.record(league, events)
        except Exception as e:
            log.warning("price history not recorded for %s: %s", league, e)
        try:
            self.steam.record(league, events)
        except Exception as e:
            log.warning("line history not recorded for %s: %s", league, e)

    def _guard(self, cands: list) -> list:
        """Pre-match: leave out, for now, the candidates of a game with fresh injury news or a Pinnacle line that is still
        moving. They are not recorded as seen or sent, so they come back on a later scan once the game is quiet."""
        if not cands:
            return cands
        out, held, cache = [], {}, {}
        for c in cands:
            k = (c.get("league"), c.get("home"), c.get("away"))
            if k not in cache:
                cache[k] = self._hold_reason(c)
            why = cache[k]
            if why:
                held[k] = (held.get(k, (why, 0))[0], held.get(k, (why, 0))[1] + 1)
                continue
            out.append(c)
        for k, (why, n) in held.items():
            log.info("pre-game guard held %d candidate(s) for %s vs %s (%s): %s", n, k[1], k[2], k[0], why)
        return out

    def _hold_reason(self, c):
        lg, home, away = c.get("league"), c.get("home"), c.get("away")
        try:
            r = self.injuries.hold_reason(lg, home, away)
            if r:
                return r
        except Exception as e:
            log.warning("injury check skipped: %s", e)
        try:
            pin = self.pinref.match(dict(league=lg, home=home, away=away, commence=c.get("commence")))
            if pin:
                return self.steam.moving(lg, _steam_key(pin))
        except Exception as e:
            log.warning("line-movement check skipped: %s", e)
        return None

    def _rest_note(self, p):
        """One plain-English rest line for a pre-match alert (back-to-back, 3rd game in 4 days, rest gap), or None."""
        pin = self.pinref.match(dict(league=p["league"], home=p["home"], away=p["away"], commence=p.get("commence")))
        if not pin:
            return None
        tip = _naive(pin.get("commence"))
        if tip is None:
            return None
        lg = p["league"]
        return restflag.rest_flag(self.teamsched.starts(lg, pin["home"]), self.teamsched.starts(lg, pin["away"]),
                                  tip, p["home"], p["away"])

    def _group_alerts(self) -> bool:
        """True when alerts go out as ONE message per game (the default Telegram notifier). A custom notify callback
        (tests, other front-ends) keeps the one-message-per-pick behaviour."""
        from core import telegram_notify
        return self.notify is telegram_notify.notify and os.environ.get("ALERT_GROUPING", "1").strip() != "0"

    def _send_groups(self, picks: list):
        from core import telegram_notify
        groups = {}
        for p in picks:
            groups.setdefault((p.get("mode"), p.get("league"), p.get("event_id")), []).append(p)
        for g in groups.values():
            try:
                if telegram_notify.notify_game(g) is not False:
                    for p in g:
                        repo.mark_sent(p["id"])
                else:
                    log.warning("picks %s saved but NOT delivered", [p["id"] for p in g])
            except Exception as e:
                log.warning("game alert failed: %s", e)

    def _events(self):
        # CLAUDE FIX: one failing league feed no longer aborts the scan for every other league
        # CLAUDE FIX: only games that start within DAYS_AHEAD days (no December games)
        for lg in LEAGUE_KEYS:
            try:
                evs = list(soft_feed.upcoming(lg))
            except Exception as e:
                log.warning("feed failed for %s: %s", lg, e)
                continue
            for ev in evs:
                if within_horizon(ev.get("commence")):
                    yield ev

    def _shrink_sim(self, ev, cands):
        """Blend each Sim probability toward Pinnacle's own devigged price of the SAME line; no matching line = no pick.
        A game with no Pinnacle entry at all is left as it is (the REQUIRE_REAL_PIN switch decides about those)."""
        try:
            from core.pinref import fair_probs
            match = getattr(self.pinref, "match", None)
            pin = match(ev) if match else None
            if not pin:
                return cands
            return gates.shrink_sim(cands, fair_probs(pin))
        except Exception as e:
            log.warning("sim shrink skipped for %s vs %s: %s", ev.get("home"), ev.get("away"), e)
            return cands

    def _has_pin(self, ev) -> bool:
        try:
            return self.pinref.match(ev) is not None
        except Exception:
            return False

    def _pin_near(self, ev, limit=3) -> list:
        """Diagnostics only: Pinnacle games of the same league starting within 36h of this SportyBet game."""
        try:
            c = _naive(ev.get("commence"))
            blob = self.pinref.data["leagues"].get(ev.get("league")) or {}
            out = []
            for p in blob.get("events", []):
                pc = _naive(p.get("commence"))
                if c and pc and abs(c - pc) <= timedelta(hours=36):
                    out.append("%s vs %s" % (p.get("home"), p.get("away")))
            return out[:limit]
        except Exception:
            return []

    def _pin_spans(self) -> str:
        """Diagnostics only: how many Pinnacle games each keyed league has cached, and from when to when."""
        try:
            spans = []
            for lg in LEAGUE_KEYS:
                if not _has_key(lg):
                    continue
                evs = (self.pinref.data["leagues"].get(lg) or {}).get("events", [])
                cs = sorted(c for c in (_naive(e.get("commence")) for e in evs) if c)
                if cs:
                    spans.append("%s %d games %s to %s" % (lg, len(evs), cs[0].strftime("%d %b %H:%M"), cs[-1].strftime("%d %b")))
            return "; ".join(spans) or "empty"
        except Exception:
            return "unavailable"

    def run_prematch(self) -> list:
        """SportyBet (soft) vs the REAL Pinnacle price of the same game. Leagues without a Pinnacle
        reference produce no picks (derived tier medians are not a price to bet against)."""
        cands, n_ev, n_pin, per_lg, missed, near, upcoming, game_list = [], 0, 0, {}, [], [], [], []
        for lg in LEAGUE_KEYS:
            try:
                self.pinref.ensure_fresh(lg)
            except Exception as e:  # out of credits / HTTP error: fall back to cached data if still fresh enough
                log.warning("pinnacle refresh failed for %s: %s", lg, e)
            try:
                evs = list(soft_feed.upcoming(lg))
            except Exception as e:
                log.warning("feed failed for %s: %s", lg, e)
                continue
            for ev in evs:
                if not within_horizon(ev.get("commence")):
                    continue
                n_ev += 1
                has = self._has_pin(ev)
                n_pin += has
                game_list.append(dict(league=lg, home=ev.get("home"), away=ev.get("away"), commence=_naive(ev.get("commence")),
                                      priced=bool(has)))
                if has and ev.get("commence"):
                    upcoming.append((_naive(ev.get("commence")), ev.get("home"), ev.get("away")))
                row = per_lg.setdefault(lg, [0, 0])
                row[0] += 1
                row[1] += has
                if not has and _has_key(lg):
                    missed.append("%s: %s vs %s [Pinnacle near that time: %s]" % (
                        lg, ev.get("home"), ev.get("away"), "; ".join(self._pin_near(ev)) or "nothing"))
                try:
                    new = self.pinref.candidates(ev, near=near)
                    for c in new:
                        c["pin_real"] = True  # these candidates are priced from a real Pinnacle line
                    cands += new
                except Exception as e:  # skip a bad event, keep the rest
                    log.warning("prematch skipped %s vs %s: %s", ev.get("home"), ev.get("away"), e)
        final = finalize(self._guard(cands), PREMATCH)   # tracking below still sees every candidate
        final = self._drop_held_markets(final)
        if _tracker.enabled():
            final = []   # the Match Tracker (core/tracker.py) sends the pre-match picks now: one growing message per match
        picks = self._persist(final)
        try:
            self._track_positions(cands)
        except Exception as e:   # tracking must never break a scan
            log.warning("position tracking failed: %s", e)
        try:
            sent_keys = {(p["league"], p["event_id"], p["market"], p["selection"], p.get("line")) for p in picks}
            self.stats.record_prematch(
                n_ev, n_pin, per_lg, len(cands), len(picks),
                [near_miss(c) for c in near if (c["league"], c["event_id"], c["market"], c["selection"], c.get("line")) not in sent_keys],
                sorted((u for u in upcoming if u[0]), key=lambda x: x[0]), games=game_list)
            self.closecalls.update("prematch", [near_miss(c) for c in near if (c["league"], c["event_id"], c["market"], c["selection"], c.get("line")) not in sent_keys])
        except Exception as e:  # the summary must never break a scan
            log.warning("digest stats (prematch) failed: %s", e)
        log.info("prematch scan: %d games in the %g-day window, %d with a real Pinnacle price, %d candidates, %d sent",
                 n_ev, days_ahead(), n_pin, len(cands), len(picks))
        log.info("prematch per league (games/with Pinnacle): %s",
                 ", ".join("%s %d/%d" % (k, v[0], v[1]) for k, v in per_lg.items()) or "none")
        log.info("pinnacle cache span: %s", self._pin_spans())
        if missed:  # leagues that DO have a Pinnacle feed but the game found no match: a name or time mismatch
            log.info("prematch no Pinnacle match in a keyed league (%d): %s", len(missed), " || ".join(missed[:10]))
        return picks

    def run_grader(self):
        """Grade finished games from their final scores (scheduler, every GRADE_EVERY_MIN)."""
        if _grader.enabled():
            return self.grader.tick()

    def _morning_report(self):
        """08:00 Lagos: yesterday's report, once a day."""
        from core import dayreport
        import time as _t
        meta = self.shadow.meta()
        now = _t.time()
        if not dayreport.due(now, meta.get("report")):
            return
        from core.tracker import _local
        meta["report"] = _local(now).strftime("%Y%m%d")
        self.shadow.flush()
        picks = repo.all_picks(3000)
        try:
            grades = self.grader._grades()
        except Exception:
            grades = {}
        try:
            bank = repo.bankroll_current()
        except Exception:
            bank = None
        from core import telegram_notify as tn
        tn.send(dayreport.build(now, picks, grades, self.shadow, bank, tickets=self.tracker.blocks, quarters=self.qgrade,
                                health=self._key_health()))

    def run_tracker(self):
        """Match Tracker tick (scheduler, every TRACK_EVERY_SEC)."""
        if _tracker.enabled() and not self.breaker_tripped():
            self.tracker.tick()
        try:
            self.finals.tick()
        except Exception as e:
            log.warning("finals tick failed: %s", e)
        try:
            if self.qgrade.grade():
                self.refresh_quarter_cards()
        except Exception as e:
            log.warning("quarter grading failed: %s", e)
        try:
            self._settle_tickets()
        except Exception as e:
            log.warning("ticket grading failed: %s", e)
        try:
            self._morning_report()
        except Exception as e:
            log.warning("daily report failed: %s", e)
        try:
            self._morning_agenda()
        except Exception as e:
            log.warning("morning match list failed: %s", e)

    def _morning_agenda(self):
        """08:00 Lagos: the day's match list, league by league, as cards that are then edited all day (new games added,
        Pinnacle ticks, started games marked). Refreshed at most every AGENDA_REFRESH_MIN (default 30) minutes."""
        from core import agenda
        import time as _t
        meta = self.shadow.meta()
        now = _t.time()
        from core.tracker import _local
        day = _local(now).strftime("%Y%m%d")
        st = meta.get("agenda_games") or {}
        if st.get("day") != day:
            if not agenda.due(now, meta.get("agenda")):
                return
            st = dict(day=day, games=[], t=0)
        else:
            try:
                gap = float(os.environ.get("AGENDA_REFRESH_MIN", 30)) * 60
            except ValueError:
                gap = 1800.0
            if not agenda.enabled() or now - st.get("t", 0) < gap:
                return
        events = list(self.tracker._events())
        st["games"] = agenda.collect(now, events, st.get("games") or [])
        st["t"] = now

        def priced(g):
            try:
                return bool(self.pinref.match(dict(league=g["lg"], home=g["home"], away=g["away"], commence=g.get("c"))))
            except Exception:
                return False
        for i, text in enumerate(agenda.render(now, st["games"], priced)):
            self.cards.put("agenda:%d" % i, text, day)
        meta["agenda"] = day
        meta["agenda_games"] = st
        self.shadow.flush()

    def _key_health(self):
        """One line for the 8am report: Odds API keys working / resting and OddsPapi calls today."""
        from config.settings import settings
        from services import odds_api as _oa
        parts = []
        try:
            n, ok, dead = _oa.book().summary(list(settings.ODDS_API_KEYS))
            if n:
                parts.append("Odds API keys: %d of %d working%s" % (ok, n, (" (%d resting until their credits refill)" % dead) if dead else ""))
        except Exception:
            pass
        try:
            pr = self.pinref
            parts.append("OddsPapi calls today: %d of %d" % (pr._papi_spent_today(), pr.papi_daily_cap))
        except Exception:
            pass
        return " | ".join(parts) or None

    def run_closing_sweep(self):
        """Every 30 s (own thread): while a live game is in its last minutes, read the SportyBet live feed again so the last
        score kept for grading is within seconds of the end (the normal scan is every 2 min). Does nothing otherwise."""
        if not self.finals.closing():
            return
        from core.bookmakers import sportybet as sb
        from core.bookmakers.soft_feed import _run

        async def one():
            return sb.parse_live(await sb._live_payload(), None)
        self.finals.observe(_run(one()))

    def result_of_pick(self, pid):
        """'won' | 'lost' | 'void' | None (grader results first, then the pick status)."""
        r = (self.grader._grades().get(str(pid)) or {}).get("res")
        if r in ("won", "lost", "void"):
            return r
        st = (repo.get_pick(pid) or {}).get("status")
        return st if st in ("won", "lost", "void") else None

    def _settle_tickets(self):
        """Suggested tickets of the block cards: graded from their legs' results (grader results first, then the pick status)."""
        b = self.tracker.blocks
        if b is None:
            return
        grades = self.grader._grades()

        def result_of(pid):
            r = (grades.get(str(pid)) or {}).get("res")
            if r in ("won", "lost", "void"):
                return r
            st = (repo.get_pick(pid) or {}).get("status")
            return st if st in ("won", "lost", "void") else None
        b.settle(result_of)

    def _drop_held_markets(self, picks: list) -> list:
        """A market family (kind + period) of a game that already has an open pre-match position is never sent again, even
        at another line or on the other side: a change of mind goes through the position tracker (hold time, urgent alert)."""
        if not positions.enabled():
            return picks
        out = []
        for p in picks:
            try:
                held = self.positions.conflicts_new(p)
            except Exception:
                held = None
            if held is not None and not p.get("switch_note"):
                log.info("prematch pick dropped: %s %s %s %s already held (#%s)", p.get("home"), p.get("market"),
                         p.get("selection"), p.get("line"), held.get("id"))
                continue
            out.append(p)
        return out

    def _track_positions(self, cands: list):
        """After each pre-match scan: re-price every open position against the newest Pinnacle data and send the urgent
        alerts (EXIT / SWITCH / WATCH). A SWITCH is also sent as a normal pick of its own."""
        if not positions.enabled() or not self.positions.active():
            return
        ok = [c for c in cands if gates.passes_win_prob(c, PREMATCH) and (c.get("ev") or 0) <= gates.max_ev(PREMATCH) + 1e-9]
        alerts = self.positions.check(self.pinref, ok, get_pick=repo.get_pick)
        if not alerts:
            return
        from core import telegram_notify
        for a in alerts:
            try:
                if a["status"] == "switch" and a.get("new"):
                    new = dict(a["new"], pin_real=True,
                               switch_note="replaces #%s: %s" % (a["pos"]["id"], a["detail"]))
                    sw = finalize([new], PREMATCH)
                    if not sw:   # the new side did not pass the full rules after all: only warn
                        a = dict(a, status="exit", detail=a["detail"] + " (the other side is not strong enough to bet)")
                        telegram_notify.send(telegram_notify.format_position_alert(a))
                    else:
                        # the urgent notice goes first, then the new side as an ordinary pick (with buttons and an id)
                        telegram_notify.send(telegram_notify.format_position_alert(dict(a, new=sw[0])))
                        self._persist(sw)
                    continue
                telegram_notify.send(telegram_notify.format_position_alert(a))
            except Exception as e:
                log.warning("position alert failed: %s", e)

    def run_sim(self) -> list:
        cands, cache, n_ev, n_pin = [], {}, 0, 0
        for ev in self._events():
            lg = ev["league"]
            n_ev += 1
            try:
                params = self.pinref.pregame_params(ev)  # real Pinnacle lines of THIS game
                if params is None and require_real_pin():
                    continue  # CLAUDE FIX: no real Pinnacle lines = no sim pick (tier priors are not a price)
                n_pin += params is not None
                new = candidates_sim(ev, _idx(cache, lg), n=MC_SIMS, params=params)
                new = self._shrink_sim(ev, new)
                for c in new:
                    c["pin_real"] = params is not None
                cands += new
            except Exception as e:
                log.warning("sim skipped %s vs %s: %s", ev.get("home"), ev.get("away"), e)
        picks = self._persist(finalize(cands, SIM))
        try:
            self.stats.record_sim(n_ev, n_pin, len(cands), len(picks))
            self.closecalls.update("sim", self._near_from(cands, picks, "sim"))
        except Exception as e:
            log.warning("digest stats (sim) failed: %s", e)
        log.info("sim scan: %d games in the %g-day window, %d with real Pinnacle lines, %d candidates, %d sent",
                 n_ev, days_ahead(), n_pin, len(cands), len(picks))
        return picks

    def match_check(self, query: str, per_game: int = 5, max_games: int = 4) -> str:
        """/match <team or league>: the best markets of the upcoming games that match, read from the same Pinnacle and
        SportyBet data the scan uses. Nothing is sent as an alert and nothing is saved; a row is marked 'qualifies' when
        it passes the same edge and win-probability rules as a Pre-Match Value alert, 'close' otherwise."""
        import html as _h
        from core import reports
        from core.pinref import _n
        from core.telegram_notify import describe, _mlabel
        e = _h.escape
        q = _n(query or "")
        if not q:
            return ("\U0001F4C5 <b>MATCH CHECK</b>\nSend a team or league, e.g. <b>/match wonju</b> or <b>/match kbl</b>.\n"
                    "It shows the best markets for the coming games that match.")
        found = []
        for lg in LEAGUE_KEYS:
            try:
                evs = list(soft_feed.upcoming(lg))
            except Exception as ex:
                log.warning("match check: feed failed for %s: %s", lg, ex)
                continue
            for ev in evs:
                if not within_horizon(ev.get("commence")):
                    continue
                hay = " ".join([_n(str(ev.get("home"))), _n(str(ev.get("away"))), _n(str((LEAGUES.get(lg) or {}).get("name", ""))), lg])
                if all(t in hay for t in q.split()):
                    found.append((lg, ev))
        found.sort(key=lambda t: (_naive(t[1].get("commence")) or _now(), t[0]))
        if not found:
            return "\U0001F4C5 <b>MATCH CHECK</b>\nNo upcoming game found for <b>%s</b> in the next %g days." % (e(query.strip()), days_ahead())
        need_ev, need_p = settings.MIN_EV, gates.min_win_prob("prematch")
        out = ["\U0001F4C5 <b>MATCH CHECK</b> \u00B7 %s" % e(query.strip()), reports.RULE]
        for lg, ev in found[:max_games]:
            c = _naive(ev.get("commence"))
            when = reports.to_local(c).strftime("%d %b %H:%M") + " " + reports.wat_label() if c else ""
            has = False
            try:
                has = bool(self._has_pin(ev))
            except Exception:
                pass
            out += ["", "<b>%s</b> \u00B7 %s vs %s" % (e(lg.upper()), e(str(ev.get("home"))), e(str(ev.get("away")))),
                    "%s \u00B7 %s" % (when, "Pinnacle \u2705" if has else "Pinnacle \u274C")]
            if not has:
                out.append("<i>No Pinnacle price for this game yet, so the engine cannot check its markets.</i>")
                continue
            near = []
            try:
                self.pinref.candidates(ev, near=near)
            except Exception as ex:
                log.warning("match check failed for %s vs %s: %s", ev.get("home"), ev.get("away"), ex)
            rows = {}
            for x in near:
                k = (x["market"], x["selection"], x.get("line"))
                rows[k] = x
            ranked = sorted(rows.values(), key=lambda x: (not ((x.get("ev") or 0) >= need_ev - 1e-9 and (x.get("p") or 0) >= need_p - 1e-9),
                                                           -gates.score(x)))
            if not ranked:
                out.append("<i>No market is near the rules right now.</i>")
                continue
            for i, x in enumerate(ranked[:per_game], 1):
                ok = (x.get("ev") or 0) >= need_ev - 1e-9 and (x.get("p") or 0) >= need_p - 1e-9
                try:
                    what = "%s \u00B7 %s" % (_mlabel(x), describe(x))
                except Exception:
                    what = "%s %s" % (x.get("market"), x.get("selection"))
                out.append("%d. %s <b>%s</b> @%.2f \u00B7 win chance %d%% \u00B7 edge %+.1f%%" % (
                    i, "\u2705" if ok else "\U0001F536", e(what), x.get("odds") or 0, round(100 * (x.get("p") or 0)), 100 * (x.get("ev") or 0)))
        if len(found) > max_games:
            out += ["", "+%d more game(s): narrow it with a team name." % (len(found) - max_games)]
        out += ["", "\u2705 passes the rules (an alert follows when it holds on two scans) \u00B7 \U0001F536 close, not yet",
                "<i>A check of what the engine sees now. Not an alert, nothing is saved.</i>"]
        return "\n".join(out)

    def livepin_check(self, arg: str = "") -> str:
        """/livepin [league]: do OddsPapi's Pinnacle prices for games in play move, or are they frozen at tip-off?"""
        from core import livepin
        leagues = [arg.strip().lower()] if arg.strip() else None
        rows, notes = self.pinref.live_probe(leagues)
        return livepin.probe_text(rows, notes)

    def _live_pin_verify(self, picks):
        """Compare each live pick with the CURRENT Pinnacle price of its game when OddsPapi has one (core/livepin.py).
        LIVE_PIN_VERIFY=off|soft|strict. A failure never blocks the scan: soft keeps the pick, strict drops it."""
        from core import livepin
        md = livepin.mode()
        if md == "off" or not picks:
            return picks
        kept, memo = [], {}
        for p in picks:
            try:
                k = (p.get("league"), p.get("home"), p.get("away"))
                if k not in memo:
                    live_ev, why = self.pinref.live_event(p)
                    memo[k] = (live_ev, self.pinref.match(p, allow_started=True), why)
                live_ev, pre, why = memo[k]
                act, q, reason = livepin.verify(p, live_ev, pre, md)
                if act == "drop":
                    log.info("live pick dropped by the live Pinnacle check: %s vs %s %s %s %s (%s)", p.get("home"), p.get("away"),
                             p.get("market"), p.get("selection"), p.get("line"), reason)
                    continue
                if q.get("pin_live") is False and why:
                    q["pin_live_note"] = why
                kept.append(q)
            except Exception as e:
                log.warning("live Pinnacle check failed for %s vs %s: %s", p.get("home"), p.get("away"), e)
                if md != "strict":
                    kept.append(p)
        return kept

    def run_live(self, top_n: int = None) -> list:
        picks = self.live_scanner.scan_once()
        try:
            self._quarter_phases()
        except Exception as e:
            log.warning("quarter phase step failed: %s", e)
        try:
            self.closecalls.update("live", self._near_from(getattr(self.live_scanner, "last_near", []), picks, "live"))
        except Exception as e:
            log.warning("close calls (live) failed: %s", e)
        try:
            self._live_seen = (self.live_scanner.last_games, self.live_scanner.last_priced, self.live_scanner.last_cands)
        except Exception:
            self._live_seen = (0, 0, 0)
        if require_real_pin():
            # CLAUDE FIX: live picks need real Pinnacle lines of the same game too
            kept = []
            for p in picks:
                try:
                    if self.pinref.pregame_params(p) is not None:
                        p["pin_real"] = True
                        kept.append(p)
                except Exception as e:
                    log.warning("live pick dropped (pinnacle check failed): %s", e)
            picks = kept
        _lpv = getattr(self, "_live_pin_verify", None)
        if _lpv is not None:
            picks = _lpv(picks)
        if stance.enabled() and getattr(self, "stances", None) is not None:
            # one stance per game: a side is only sent after a stable read, never contradicts the stance, and the opposite
            # side only goes out as a labelled REVERSAL (core/stance.py)
            try:
                picks, held = stance.gate(self.stances, picks, seeds_for=self._pre_seeds)
                if held:
                    log.info("live stance gate held %d pick(s): %s", len(held), "; ".join(
                        "%s %s %s (%s)" % (h.get("home"), h.get("market"), h.get("selection"),
                                           (h.get("stance_decision") or {}).get("reason")) for h in held[:6]))
            except Exception as e:
                log.warning("stance gate failed, no live picks this scan: %s", e)
                picks = []
        # CLAUDE FIX: at most ONE live pick per game (the best EV), never a stack of lines on one game
        best = {}
        for p in picks:
            k = (p.get("league"), p.get("home"), p.get("away"))
            if k not in best or (p.get("ev") or 0) > (best[k].get("ev") or 0):
                best[k] = p
        picks = list(best.values())
        if not live_enabled():
            # emergency switch only (LIVE_ENABLED=0): nothing is saved or sent
            for p in picks:
                log.info("LIVE LOG-ONLY %s %s vs %s %s %s %s @%s ev=%.3f", p.get("league"), p.get("home"), p.get("away"),
                         p.get("market"), p.get("selection"), p.get("line"), p.get("odds"), p.get("ev") or 0)
            return []
        if top_n is not None:
            picks = sorted(picks, key=lambda p: p.get("ev", 0), reverse=True)[:max(0, int(top_n))]
        sent = self._persist(picks)
        try:
            from core.filter import remember_sent
            remember_sent("live", sent)
        except Exception as e:
            log.warning("live memory update failed: %s", e)
        try:
            g, pr, c = self._live_seen
            self.stats.record_live(g, pr, c, len(sent))
        except Exception as e:
            log.warning("digest stats (live) failed: %s", e)
        return sent

    def _quarter_phases(self):
        """Once per game per phase, send the forecast for the quarters / half still to come (see core.quarters)."""
        if os.environ.get("QUARTER_PHASE", "1").strip() == "0":
            return
        import time as _t
        from core import quarters, telegram_notify
        now = _t.time()
        self._quarter_load(now)
        before = (len(self._quarter_sent), dict(self._ht_scores), dict(self._q3_scores), dict(self._q1_scores))
        for k in [k for k, t in self._quarter_sent.items() if now - t > 12 * 3600]:
            del self._quarter_sent[k]
        for k in [k for k, t in self._ht_seen.items() if now - t > 12 * 3600]:
            self._ht_seen.pop(k, None)
            self._ht_scores.pop(k, None)
            self._q3_scores.pop(k, None)
            self._q1_scores.pop(k, None)
        for it in list(getattr(self.live_scanner, "outlooks", []) or []):
            ph = quarters.phase_of(it)
            gk = (it.get("league"), it.get("home"), it.get("away"))
            if isinstance(ph, str):   # a break: the score is where the next period starts (exact); no message at a break
                tot = (it.get("home_score") or 0) + (it.get("away_score") or 0)
                if ph == "HT":
                    self._ht_scores[gk] = tot
                    self._ht_seen[gk] = now
                elif ph == "B1":
                    self._q1_scores[gk] = tot
                    self._ht_seen[gk] = now
                elif ph == "B3":
                    self._q3_scores[gk] = tot
                    self._ht_seen[gk] = now
                continue
            key = gk + (ph,)
            if ph is None or key in self._quarter_sent:
                continue
            if str(ph) not in quarters.sent_phases():   # QUARTER_PHASES: default only Q1 and Q3 (Q2 / Q4 repeat them)
                continue
            msg = quarters.phase_message(it, ht_total=self._ht_scores.get(gk), q3_total=self._q3_scores.get(gk), q1_total=self._q1_scores.get(gk))
            if not msg:
                continue
            self._quarter_sent[key] = now
            try:
                self.qgrade.record(gk, ph, _qgrade.forecasts(it, ph, self._ht_scores.get(gk)))
            except Exception as e:
                log.warning("quarter forecast not recorded: %s", e)
            try:
                fc = _qgrade.forecasts(it, ph, self._ht_scores.get(gk)) or {}
                self._quarter_card(gk, str(ph), msg, _qcard.take_lines(fc, it.get("qlines") or {}))
            except Exception as e:
                log.warning("quarter card failed: %s", e)
        try:
            self.qgrade.observe(q1=self._q1_scores, ht=self._ht_scores, q3=self._q3_scores)
        except Exception as e:
            log.warning("quarter breaks not copied: %s", e)
        if (len(self._quarter_sent), dict(self._ht_scores), dict(self._q3_scores), dict(self._q1_scores)) != before:
            self._quarter_save()

    def _quarter_card(self, gk, ph, msg=None, take=None):
        """The game's one QUARTERS card: add this phase's section (when given) and send / edit it."""
        import time as _t
        from core.tracker import _local
        now = _t.time()
        key = "q:%s|%s|%s" % gk
        c = self.cards.get(key)
        data = dict(c.get("data") or {}) if c else {}
        data.setdefault("sec", {})
        data.setdefault("take", {})
        if msg:
            data["sec"][ph] = msg
            data["take"][ph] = take or []
        if not data["sec"]:
            return
        day = c["day"] if c and now - c.get("t", 0) < 8 * 3600 else _local(now).strftime("%Y%m%d")
        text = _qcard.render(gk[0], gk[1], gk[2], data, self.qgrade.game(gk), self.qgrade.counts().get("graded", 0))
        self.cards.put(key, text, day, silent=False if msg else True, data=data)

    def refresh_quarter_cards(self):
        """After grading: update the cards of games whose forecast results just came in (RESULT CHECK section)."""
        for key, c in list((self.cards._st() or {}).items()):
            if not key.startswith("q:") or not (c.get("data") or {}).get("sec"):
                continue
            try:
                lg, h, a = key[2:].split("|", 2)
                self._quarter_card((lg, h, a), "")
            except Exception as e:
                log.warning("quarter card refresh failed: %s", e)

    def _quarter_load(self, now):
        """First use after a start: read back the half-time scores and the phases already sent (kv 'quarter_state')."""
        if self._quarter_loaded:
            return
        self._quarter_loaded = True
        try:
            st = json.loads(repo.kv_get("quarter_state") or "{}")
            for lg, h, a, tot, ts in st.get("ht", []):
                if now - ts <= 12 * 3600:
                    self._ht_scores[(lg, h, a)] = tot
                    self._ht_seen[(lg, h, a)] = ts
            for lg, h, a, tot, ts in st.get("q1", []):
                if now - ts <= 12 * 3600:
                    self._q1_scores[(lg, h, a)] = tot
                    self._ht_seen.setdefault((lg, h, a), ts)
            for lg, h, a, tot, ts in st.get("q3", []):
                if now - ts <= 12 * 3600:
                    self._q3_scores[(lg, h, a)] = tot
                    self._ht_seen.setdefault((lg, h, a), ts)
            for lg, h, a, ph, ts in st.get("sent", []):
                if now - ts <= 12 * 3600:
                    self._quarter_sent[(lg, h, a, ph)] = ts
        except Exception as e:
            log.warning("quarter state not restored: %s", e)

    def _quarter_save(self):
        try:
            st = dict(ht=[[k[0], k[1], k[2], v, self._ht_seen.get(k, 0)] for k, v in self._ht_scores.items()],
                      q1=[[k[0], k[1], k[2], v, self._ht_seen.get(k, 0)] for k, v in self._q1_scores.items()],
                      q3=[[k[0], k[1], k[2], v, self._ht_seen.get(k, 0)] for k, v in self._q3_scores.items()],
                      sent=[[k[0], k[1], k[2], k[3], t] for k, t in self._quarter_sent.items()])
            repo.kv_set("quarter_state", json.dumps(st))
        except Exception as e:
            log.warning("quarter state not saved: %s", e)

    def _pre_seeds(self, p: dict) -> dict:
        """Pre-match alerts already sent for this game -> the side the live stance starts from (full-game picks only)."""
        import time as _t
        from core.pinref import _same
        k = stance.game_key(p)
        hit = self._seed_cache.get(k)
        if hit and _t.time() - hit[0] < 600:
            return hit[1]
        try:
            rows = repo.prematch_picks_for(p.get("league"), p.get("home"), p.get("away"))
        except Exception as e:
            log.warning("pre-match seeds unavailable: %s", e)
            return {}
        rows = [r for r in rows if _same(r.get("home"), p.get("home")) and _same(r.get("away"), p.get("away"))]
        seeds = stance.seeds_from_picks(rows)
        self._seed_cache[k] = (_t.time(), seeds)
        return seeds

    # --- hourly reports ---
    def _near_from(self, cands: list, sent: list, mode: str) -> list:
        """Candidates that were NOT sent but are near the bar (edge within 3 points below zero or above), for 'close calls'."""
        from core.ev_engine import edge
        from core.digest import NEAR_FLOOR
        sent_keys = {(p.get("league"), p.get("event_id"), p.get("market"), p.get("selection")) for p in sent or []}
        out = []
        for c in cands or []:
            try:
                p, odds = c.get("p"), c.get("odds")
                if p is None or not odds:
                    continue
                ev = c.get("ev") if c.get("ev") is not None else edge(p, odds)
                if ev < NEAR_FLOOR or ev > gates.max_ev(mode) + 1e-9:
                    continue
                if (c.get("league"), c.get("event_id"), c.get("market"), c.get("selection")) in sent_keys:
                    continue
                out.append(near_miss(dict(c, ev=ev)))
            except Exception:
                continue
        out.sort(key=lambda x: x["ev"], reverse=True)
        return out[:30]

    def build_reports(self, reset: bool = True, now=None) -> list:
        """The hourly reports as a list of texts: games, one close-calls report per mode that has entries, engine status."""
        from datetime import datetime, timezone
        from core import reports
        now = now or datetime.now(timezone.utc)
        snap = self.stats.snapshot_and_reset(reset=reset)
        out = []
        pre = snap.get("pre") or {}
        base = None
        try:
            raw = repo.kv_get("report_games_baseline")
            base = json.loads(raw) if raw else None
        except Exception as e:
            log.warning("games baseline unreadable: %s", e)
        text, new_base = reports.games_report(pre.get("game_list") or [], base, now, snap["n_pre"], snap["n_sim"], snap["n_live"],
                                              snap.get("window_min", 60))
        out.append(text)
        if reset and new_base is not base:
            try:
                repo.kv_set("report_games_baseline", json.dumps(new_base))
            except Exception as e:
                log.warning("games baseline not saved: %s", e)
        for mode in ("prematch", "live", "sim"):
            t = reports.close_calls_report(self.closecalls, mode, now)
            if t:
                out.append(t)
        out.append(reports.status_report(snap, gather_ctx(self), now))
        return out

    def build_digest(self, reset: bool = True) -> str:
        return "\n\n".join(self.build_reports(reset=reset))

    @staticmethod
    def _link_notice(lines: list) -> str:
        """'bsl: LINKED to Turkiye Super Lig tid=519 - 4 game(s) with Pinnacle prices; markets: h2h, spreads' -> a readable notice."""
        import re
        import html as _h
        out = ["\U0001F195 <b>NEW PINNACLE SOURCE LINKED</b>"]
        for ln in lines:
            m = re.match(r"^(\w+): LINKED to (.+?) tid=(\d+) - (\d+) game\(s\) with Pinnacle prices; markets: (.*)$", ln.strip())
            if not m:
                out.append(_h.escape(ln))
                continue
            code, name, tid, n, mk = m.groups()
            title = (LEAGUES.get(code) or {}).get("name") or code.upper()
            out += ["%s \u2192 %s (tid %s)" % (_h.escape(title), _h.escape(name), tid),
                    "%s game%s priced \u00B7 markets: %s" % (n, "" if n == "1" else "s", _h.escape(mk))]
        return "\n".join(out)

    def auto_link(self) -> str:
        """Every few hours: leagues that still have no Pinnacle source are looked up again (see core.leagues_dyn.linkall;
        links only when unambiguous and verified). Silent unless something NEW gets linked."""
        from core import leagues_dyn
        todo = [c for c in leagues_dyn.LINK_RULES if c in LEAGUES and not LEAGUES[c].get("odds_api_key") and not LEAGUES[c].get("oddspapi_tid")]
        if not todo:
            return ""
        try:
            rows = self.pinref.tournaments()
            text = leagues_dyn.linkall(rows, self.pinref.probe, repo.kv_get, repo.kv_set)
        except Exception as e:
            log.info("auto-link skipped: %s", e)
            return ""
        new = [ln for ln in text.splitlines() if ": LINKED" in ln]
        if new and self.notify is not None:
            from core import telegram_notify
            telegram_notify.send(self._link_notice(new))
        log.info("auto-link: %d new link(s)", len(new))
        return text

    def wrapup_text(self, now=None) -> str:
        """Today's (local day) bets in a few lines: placed, won, lost, open, profit, bankroll, alerts left alone."""
        from datetime import datetime, timezone, timedelta
        from core.digest import _tz
        now = now or datetime.now(timezone.utc)
        off = timedelta(hours=_tz())
        local = now + off
        start = (datetime(local.year, local.month, local.day) - off)      # naive UTC of local midnight
        end = start + timedelta(days=1)
        made = repo.created_between(start, end)
        done = repo.settled_between(start, end)

        def profit(p):
            odds = p.get("took_odds") or p.get("book_odds") or p.get("min_odds") or 1.0
            st = p.get("stake") or 0.0
            return st * (odds - 1) if p["status"] == "won" else (-st if p["status"] == "lost" else 0.0)
        won = sum(1 for p in done if p["status"] == "won")
        lost = sum(1 for p in done if p["status"] == "lost")
        net = sum(profit(p) for p in done)
        placed = sum(1 for p in made if p["status"] in ("placed", "won", "lost", "void"))
        skipped = sum(1 for p in made if p["status"] in ("new", "skipped"))
        open_n = sum(1 for p in repo.placed_bets(500) if p["status"] == "placed")
        sign = "+" if net > 0 else "-" if net < 0 else ""
        sent = {m: sum(1 for p in made if p.get("mode") == m) for m in ("prematch", "sim", "live")}
        return "\n".join(["\U0001F319 <b>DAILY WRAP-UP</b> \u00B7 %s" % local.strftime("%d %b"), "\u2501" * 15,
                          "<i>How today went for the bets you placed.</i>", "",
                          "Alerts sent: Pre-Match <b>%d</b> \u00B7 Sim <b>%d</b> \u00B7 Live <b>%d</b>" % (sent["prematch"], sent["sim"], sent["live"]),
                          "Placed <b>%d</b> \u00B7 Won <b>%d</b> \u00B7 Lost <b>%d</b> \u00B7 Open <b>%d</b>" % (placed, won, lost, open_n),
                          "Profit today: <b>%s\u20a6%s</b> \u00B7 Bankroll <b>\u20a6%s</b>" % (
                              sign, format(int(round(abs(net))), ","), format(int(round(repo.bankroll_current())), ",")),
                          "Alerts you skipped: <b>%d</b>" % skipped])

    def wrapup_if_due(self) -> str:
        """Once a day, after WRAPUP_HOUR (local time, default 23): send the wrap-up. Remembered in the database."""
        from datetime import datetime, timezone, timedelta
        from core.digest import _tz
        if self.notify is None:
            return ""
        try:
            hour = int(os.environ.get("WRAPUP_HOUR", 23))
        except ValueError:
            hour = 23
        local = datetime.now(timezone.utc) + timedelta(hours=_tz())
        day = local.strftime("%Y-%m-%d")
        if local.hour < hour or repo.kv_get("wrapup_last") == day:
            return ""
        text = self.wrapup_text()
        from core import telegram_notify
        if telegram_notify.send(text, silent=True):
            repo.kv_set("wrapup_last", day)
        return text

    def build_cards(self, reset: bool = True, now=None) -> dict:
        """The hourly cards: {'status': text, 'closecalls': text, 'warnings': [str]} (the GAMES list is the 8am card now)."""
        from datetime import datetime, timezone
        from core import reports
        now = now or datetime.now(timezone.utc)
        snap = self.stats.snapshot_and_reset(reset=reset)
        ctx = gather_ctx(self)
        return dict(status=reports.status_report(snap, ctx, now), closecalls=reports.close_calls_card(self.closecalls, now),
                    warnings=reports.status_warnings(snap, ctx))

    def send_digest(self) -> str:
        """Hourly: ONE silent ENGINE STATUS card and ONE CLOSE CALLS card, each edited in place (a new card each local day).
        A separate, audible alert goes out only for a warning that was not already raised today."""
        from core.tracker import _local
        import time as _t
        cards = self.build_cards(reset=True)
        if self.notify is not None:
            day = _local(_t.time()).strftime("%Y%m%d")
            self.cards.put("status", cards["status"], day)
            self.cards.put("closecalls", cards["closecalls"], day)
            c = self.cards.get("warned")
            told = set((c.get("data") or {}).get("w") or []) if c and c.get("day") == day else set()
            import re as _re
            norm = lambda w: _re.sub(r"\d+", "#", w)          # '(12 games)' changing to '(11 games)' is not a new warning
            new = [w for w in cards["warnings"] if norm(w) not in told]
            if new:
                from core import telegram_notify
                import html as _h
                if telegram_notify.send("\u26A0\uFE0F <b>ENGINE WARNING</b>\n" + "\n".join(_h.escape(w) for w in new)):
                    st = self.cards._st()
                    st["warned"] = dict(day=day, msg_id=0, h="", t=_t.time(), data=dict(w=sorted(told | {norm(w) for w in new})))
                    self.cards._dirty = True
                    self.cards.flush()
        return "\n\n".join([cards["status"], cards["closecalls"]])
