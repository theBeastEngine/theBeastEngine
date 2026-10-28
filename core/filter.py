"""Shared filters (one rule set for perception, validator and the engine) + finalize()."""
import logging
import time
from config.settings import settings, MAX_ODDS, MAX_BOOK_MARGIN
from core.ev_engine import rank, edge
from core.kelly import compute_stake
from core import gates
from core import confirm
from core.modes import MODES, LIVE
from core.scores.sofascore import FAKE as FAKE_WORDS

# CLAUDE FIX: prematch/sim had only the in-memory 30-min Ctx window, so the same pick was re-sent every cycle after
# 30 minutes and again after every restart. Modes without their own no_repeat_min now use a 12h DB window.
log = logging.getLogger(__name__)
DEFAULT_NO_REPEAT_MIN = 720


def _g(s, key, default=None):
    if isinstance(s, dict):
        return s.get(key, default)
    return getattr(s, key, default)


class Ctx:
    """Memory between scans: what was sent, at what price and probability."""
    WINDOW = 1800.0
    ODDS_MOVE = 0.02
    P_MOVE = 0.02

    def __init__(self, window: float = None):
        self.window = window or self.WINDOW
        self.seen = {}

    @staticmethod
    def key(s):
        return (_g(s, "league"), _g(s, "event_id"), _g(s, "market"), _g(s, "side") or _g(s, "selection"), _g(s, "line"))

    def remember(self, picks):
        now = time.time()
        for s in picks:
            self.seen[self.key(s)] = (now, _g(s, "odds"), _g(s, "p"))
        for k in [k for k, v in self.seen.items() if now - v[0] > self.window]:
            del self.seen[k]

    def unchanged(self, s) -> bool:
        rec = self.seen.get(self.key(s))
        if not rec or time.time() - rec[0] > self.window:
            return False
        _, o, p = rec
        o2, p2 = _g(s, "odds"), _g(s, "p")
        if not (o and o2 and p is not None and p2 is not None):
            return True
        return abs(o2 / o - 1) < self.ODDS_MOVE and abs(p2 - p) < self.P_MOVE


def is_fake(s) -> bool:
    names = f"{_g(s, 'home', '')} {_g(s, 'away', '')} {_g(s, 'league', '')}".lower()
    return any(w in names for w in FAKE_WORDS)


def is_liability(s) -> bool:
    """Longshot prices: tiny model errors turn into fake EV."""
    o = _g(s, "odds")
    return bool(o) and o > MAX_ODDS


def is_margin_trap(s) -> bool:
    """Book's own two-way overround is huge: an apparent edge there is usually a distorted/stale market."""
    m = _g(s, "book_margin")
    return m is not None and m > MAX_BOOK_MARGIN


def reject_reason(s, ctx=None):
    if is_fake(s):
        return "fake_game"
    if is_liability(s):
        return "liability"
    if is_margin_trap(s):
        return "margin_trap"
    return None


def filter_with_reasons(pool, ctx=None):
    kept, reasons = [], {}
    for s in pool:
        r = reject_reason(s, ctx)
        if r:
            reasons[r] = reasons.get(r, 0) + 1
        else:
            kept.append(s)
    return kept, reasons


def apply_dedup(kept, ctx=None):
    best = {}
    for s in kept:
        k = Ctx.key(s)
        if k not in best or (_g(s, "ev", 0) or 0) > (_g(best[k], "ev", 0) or 0):
            best[k] = s
    out = list(best.values())
    return [s for s in out if not (ctx and ctx.unchanged(s))]


_CTX = {}


def remember_sent(mode: str, picks: list) -> None:
    """Live: finalize(remember=False) leaves the 'already seen' memory alone, because a pick can still be held back by
    the stance gate; only what was really sent is remembered here."""
    _CTX.setdefault(mode, Ctx()).remember(picks)


def finalize(cands: list, mode: str, bankroll: float = None, ctx: Ctx = None, remember: bool = True) -> list:
    """perception filters -> rank -> DB no-repeat + per-book limit -> stake. Remembers only what it sends."""
    from storage import repository as repo
    from core.perception import perception_report
    spec = MODES[mode]
    bankroll = repo.bankroll_current() if bankroll is None else bankroll
    ctx = ctx if ctx is not None else _CTX.setdefault(mode, Ctx())
    no_repeat_min = spec.no_repeat_min or DEFAULT_NO_REPEAT_MIN
    for c in cands:
        c["ev"] = edge(c["p"], c["odds"])
        c.setdefault("side", c["selection"])
    pool, _ = perception_report(cands, live=(mode == LIVE), ctx=ctx, remember=False)
    # step 2: win-probability floor (before ranking), then best-first by EV + priority, per-game caps, no contradictions
    pool = [p for p in pool if gates.passes_win_prob(p, mode) and p["ev"] <= gates.max_ev(mode) + 1e-9]
    ranked = sorted(rank(pool, len(pool) or 1), key=gates.score, reverse=True)
    ranked = confirm.confirm(ranked, mode, ctx)  # Pre-match / Sim / Live: the edge must hold on two scans
    by_game, order = {}, []
    for p in ranked:
        g = (p["league"], p["event_id"])
        if g not in by_game:
            by_game[g] = []
            order.append(g)
        by_game[g].append(p)
    out, per_book, taken, games_sent = [], {}, {}, 0
    for g in order:  # games ordered by their best pick
        sendable = []
        for p in by_game[g]:
            if repo.recently_sent(p["league"], p["event_id"], p["market"], p["selection"], no_repeat_min):
                continue
            sendable.append(p)
        if mode == LIVE and sendable:
            try:  # per-game protection: cooldown, cap, never the opposite side of an earlier alert
                hist = list(repo.live_history(g[0], g[1], gates.LIVE_HISTORY_MIN))
            except Exception:
                hist = []
            sendable = [p for p in sendable if not gates.live_blocked(p, hist, allow_reversal=gates.reversal_candidate(p))]
        chosen = []
        for p in gates.select_for_game(sendable, mode):
            if repo.bets_today_on_book(p["book"]) + per_book.get(p["book"], 0) >= settings.MAX_BETS_PER_BOOK_PER_DAY:
                continue
            stake = compute_stake(bankroll)
            if stake <= 0:
                continue
            per_book[p["book"]] = per_book.get(p["book"], 0) + 1
            chosen.append({**p, "stake": stake, "mode": mode, "rank_in_game": len(chosen) + 1,
                           "priority": gates.priority_name(p)})
        if chosen:   # every market of one match moves together: cap the money at risk on the match as a whole
            try:
                from core.kelly import cap_match_stakes
                new = cap_match_stakes([c["stake"] for c in chosen], bankroll, repo.open_exposure(g[0], g[1]))
            except Exception:
                new = [c["stake"] for c in chosen]
            for c, st in zip(chosen, new):
                if st != c["stake"]:
                    c["stake_scaled"] = True
                    c["stake"] = st
            dropped = [c for c in chosen if c["stake"] <= 0]
            if dropped:
                log.info("match exposure cap: %d alert(s) dropped for %s", len(dropped), g)
            chosen = [c for c in chosen if c["stake"] > 0]
        if not chosen:
            continue
        chosen.sort(key=gates.send_order)   # Telegram order: full game, Q1, Q2, 1H, Q3, Q4, 2H (rank_in_game stays the EV rank)
        out += chosen
        games_sent += 1
        if mode == LIVE and games_sent >= gates.scan_cap(mode):
            break
        if mode != LIVE and len(out) >= gates.scan_cap(mode):
            out = out[:gates.scan_cap(mode)]
            break
    if remember:
        ctx.remember(out)
    return out
