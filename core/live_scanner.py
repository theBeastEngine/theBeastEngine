import difflib
import os
from core.bookmakers import soft_feed
from core.live_model import fmt, frac_remaining, live_prob, pregame_params, MIN_REMAINING
from core.market_selector import iter_outcomes, gold_index
from core import gates
from core.filter import finalize
from core.scores import sofascore
from services.ratings import matchup_type
from services.logger import get_logger

log = get_logger("live")


def _match(game, events):
    gh, ga = soft_feed.norm(game["home"]), soft_feed.norm(game["away"])
    best, score = None, 0.0
    for ev in events:
        s = (difflib.SequenceMatcher(None, gh, soft_feed.norm(ev["home"])).ratio()
             + difflib.SequenceMatcher(None, ga, soft_feed.norm(ev["away"])).ratio()) / 2
        if s > score:
            best, score = ev, s
    return best if score >= 0.8 else None


class LiveScanner:
    """Real games only (Sofascore). 0 Odds API credits: pre-game strength comes from the cached real Pinnacle
    lines of the game (pinref) when available, else from the gold table / league priors."""

    def __init__(self, pinref=None):
        self.pinref = pinref
        self._idx = {}
        self.last_games = 0
        self.last_priced = 0
        self.last_cands = 0
        self.last_near = []
        self.outlooks = []
        self._why_logged = set()

    def _params(self, ev, lg, mt):
        mu, T, sd = pregame_params(self._idx[lg], lg, mt)
        if self.pinref is not None:
            try:
                pp = self.pinref.pregame_params(ev)
            except Exception as e:
                log.warning("pinref params failed for %s vs %s: %s", ev.get("home"), ev.get("away"), e)
                pp = None
            if pp:
                mu, T = pp
        return mu, T, sd

    def _record_outlook(self, g, lg):
        if not lg:
            return
        from core import quarters
        if lg not in self._idx:
            self._idx[lg] = gold_index(lg)
        ev = _match(g, soft_feed.live_game(lg, g["home"], g["away"])) or dict(home=g["home"], away=g["away"], league=lg)
        ev.setdefault("league", lg)               # without the league the Pinnacle cache cannot be searched at all
        mt = matchup_type(lg, ev["home"], ev["away"])
        mu, T, _sd = pregame_params(self._idx[lg], lg, mt)
        src = "league"
        if self.pinref is not None:
            pm, pt, why = self.pinref.pregame_parts(ev)
            if pm is not None and pt is not None:
                mu, T, src = pm, pt, "pinnacle"
            elif pm is not None:
                mu, src = pm, "pinnacle_spread"
            elif pt is not None:
                T, src = pt, "pinnacle_total"
            gk = (lg, ev["home"], ev["away"])
            if src != "pinnacle" and gk not in self._why_logged:
                self._why_logged.add(gk)           # once per game: why a forecast is not fully on Pinnacle
                log.info("outlook %s vs %s (%s): %s -> source %s", ev["home"], ev["away"], lg, why, src)
        n, L = fmt(lg)
        o = quarters.outlook(lg, g["period"], g["elapsed"], g["home_score"], g["away_score"], mu, T, n, L,
                             halftime=bool(g.get("halftime")), break_after=g.get("break_after"), source=src)
        if o:
            rs = g.get("remaining")
            o["qlines"] = {k: v for k, v in (ev.get("markets") or {}).items() if str(k).startswith("totals_")}
            o.update(home=g["home"], away=g["away"], n=n,
                     clock=("%d:%02d" % divmod(int(rs), 60)) if rs is not None else "")
            self.outlooks.append(o)

    def candidates(self, games=None) -> list:
        games = sofascore.live_games() if games is None else games
        self.last_games = len(games)
        cands = []
        self.outlooks = []
        for g in games:
            lg = g["league"]
            try:
                self._record_outlook(g, lg)
            except Exception as e:  # the forecast must never break a scan
                log.warning("quarter outlook failed for %s: %s", g.get("home"), e)
            if not lg or g["halftime"]:
                continue
            rem = frac_remaining(lg, g["period"], g["elapsed"])
            if rem < MIN_REMAINING:
                continue
            ev = _match(g, soft_feed.live_game(lg, g["home"], g["away"]))
            if not ev:
                continue
            if lg not in self._idx:  # CLAUDE FIX: `get(lg) or setdefault(...)` re-queried the DB while the index was empty
                self._idx[lg] = gold_index(lg)
            mt = matchup_type(lg, ev["home"], ev["away"])
            mu, T, sd = self._params(ev, lg, mt)
            diff, cur = g["home_score"] - g["away_score"], g["home_score"] + g["away_score"]
            # CLAUDE FIX: live alerts must show the score, period and clock the price was quoted at
            rs = g.get("remaining")
            if rs is None:
                rs = max(fmt(lg)[1] - (g.get("elapsed") or 0), 0)
            state = {"period": g["period"], "home_score": g["home_score"], "away_score": g["away_score"],
                     "remaining_sec": int(rs)}
            for o in iter_outcomes(ev):
                if o["market"] == "totals" and rem < gates.live_totals_min_remaining():
                    continue  # late-game fouling inflates scoring: the totals model is not trustworthy there
                p = live_prob(o["market"], o["selection"], o["line"], rem, diff, cur, mu, T, sd)
                if p and p > 0:
                    cands.append({**o, "event_id": ev["event_id"], "league": lg, "home": ev["home"],
                                  "away": ev["away"], "commence": ev.get("commence"), "p": p, **state})
        # no live Pinnacle price exists: blend the model toward the book's own de-vigged live price (winner's-curse guard)
        return gates.shrink_live(cands)

    def _has_real_pin(self, c, memo) -> bool:
        k = (c.get("league"), c.get("home"), c.get("away"))
        if k not in memo:
            try:
                memo[k] = self.pinref.pregame_params(c) is not None
            except Exception as e:
                log.warning("pinnacle check failed for %s vs %s: %s", c.get("home"), c.get("away"), e)
                memo[k] = False
        return memo[k]

    def scan_once(self, games=None) -> list:
        self._idx = {}
        cands = self.candidates(games)
        n_all_before_gate = len(cands)
        # CLAUDE FIX: a game with no real Pinnacle lines must not take a top-N slot: gate BEFORE ranking
        if self.pinref is not None and os.environ.get("REQUIRE_REAL_PIN", "1").strip() != "0":
            memo = {}
            cands = [c for c in cands if self._has_real_pin(c, memo)]
        self.last_cands = len(cands)
        self.last_near = list(cands)   # before the gates: the 'close calls' report picks the ones near the bar
        self.last_priced = len({(c.get("league"), c.get("home"), c.get("away")) for c in cands})
        out = finalize(cands, "live", remember=False)   # the stance gate may still hold a pick back
        if self.last_games or n_all_before_gate:  # nothing to say when no game is live
            log.info("live scan: %d live games, %d candidates (%d with a real Pinnacle price), %d after ranking",
                     self.last_games, n_all_before_gate, len(cands), len(out))
        return out
