"""Step-2 gates: win-probability floor, no contradictory picks inside a game, per-game market caps, priority.
Pure functions (no DB, no network). Thresholds are env-adjustable and never touch the locked constants."""
import os
from config.markets import BY_KEY

DEFAULT_MIN_WIN_PROB = {"prematch": 0.55, "sim": 0.60, "live": 0.60}
DEFAULT_PER_GAME = {"prematch": 5, "sim": 3, "live": 3}   # live: the stance gate then keeps at most one alert per game per scan
# Safety nets only (number of alerts per scan; live: number of games). Pre-match / sim must never cut a game down to make
# room for another one: every game gets its own best markets (PER_GAME caps), so these are set far above what a slate needs.
DEFAULT_SCAN_CAP = {"prematch": 400, "sim": 150, "live": 10}
# priority bonus added to EV for ranking only (Totals rest on the Pinnacle total, the sharpest line; ML is the noisiest)
PRIORITY = {"total": 0.010, "spread": 0.005, "h2h": 0.0}
PRIORITY_NAME = {"total": "High", "spread": "Medium", "h2h": "Standard"}


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def min_win_prob(mode: str) -> float:
    return _env_float("MIN_WIN_PROB_" + mode.upper(), DEFAULT_MIN_WIN_PROB.get(mode, 0.55))


def per_game_cap(mode: str) -> int:
    return max(1, _env_int("PER_GAME_" + mode.upper(), DEFAULT_PER_GAME.get(mode, 1)))


def scan_cap(mode: str) -> int:
    return max(1, _env_int("SCAN_CAP_" + mode.upper(), DEFAULT_SCAN_CAP.get(mode, 10)))


def kind_period(p: dict):
    m = BY_KEY.get(p.get("market"))
    if m:
        return m.kind, m.period
    k = {"h2h": "h2h", "spreads": "spread", "totals": "total"}.get(p.get("market"), p.get("market"))
    return k, "full"


# Telegram order inside one game: the full game first, then Q1, Q2, 1st half, Q3, Q4, 2nd half; inside a period the
# Total comes before the Spread (full game: Moneyline, Spread, Total).
PERIOD_ORDER = {"full": 0, "q1": 1, "q2": 2, "h1": 3, "q3": 4, "q4": 5, "h2": 6}
_KIND_ORDER = {"h2h": 0, "spread": 1, "total": 2}
_KIND_ORDER_PERIOD = {"total": 0, "spread": 1, "h2h": 2}


def send_order(p: dict) -> tuple:
    kind, per = kind_period(p)
    ko = (_KIND_ORDER if per == "full" else _KIND_ORDER_PERIOD).get(kind, 9)
    return (PERIOD_ORDER.get(per, 9), ko)


def priority(p: dict) -> float:
    return PRIORITY.get(kind_period(p)[0], 0.0)


def priority_name(p: dict) -> str:
    return PRIORITY_NAME.get(kind_period(p)[0], "Standard")


def score(p: dict) -> float:
    return (p.get("ev") or 0) + priority(p)


def passes_win_prob(p: dict, mode: str) -> bool:
    pr = p.get("p")
    return pr is not None and pr >= min_win_prob(mode) - 1e-9


def _team(p):
    """Which team does this pick back? h2h/spread: the selection side; totals back no team."""
    kind, _ = kind_period(p)
    if kind in ("h2h", "spread"):
        return p.get("side") or p.get("selection")
    return None


def conflicts(a: dict, b: dict) -> bool:
    """True if two picks on the SAME game contradict or duplicate each other."""
    ka, pa = kind_period(a)
    kb, pb = kind_period(b)
    if pa != pb:
        # different periods (e.g. full game vs 1st half) are different bets, except a total Over vs Under is allowed
        return False
    if ka == kb:
        # one pick per kind per period: two spreads/totals/moneylines on the same game period are either
        # contradictory (opposite sides) or the same bet at another line
        return True
    if {ka, kb} == {"h2h", "spread"}:
        return _team(a) != _team(b)   # ML on one team + spread on the other team contradict
    return False


def select_for_game(ranked: list, mode: str) -> list:
    """ranked: one game's picks, best first. Returns the picks kept (<= per-game cap, no contradictions)."""
    cap = per_game_cap(mode)
    kept = []
    for p in ranked:
        if len(kept) >= cap:
            break
        if any(conflicts(p, k) for k in kept):
            continue
        kept.append(p)
    return kept


# ---- Sim / Live confirmation and Sim shrinkage -------------------------------------------------------------
DEFAULT_PERSIST_SEC = {"sim": 9000.0, "live": 360.0}   # an edge must be seen on 2 scans inside this window


def persist_window(mode: str) -> float:
    """Seconds an edge may wait for its confirming second sighting. 0 = off. Prematch is never delayed."""
    if mode not in DEFAULT_PERSIST_SEC:
        return 0.0
    return max(0.0, _env_float("PERSIST_WINDOW_" + mode.upper(), DEFAULT_PERSIST_SEC[mode]))


def confirm_persistent(ranked: list, mode: str, ctx, now: float = None) -> list:
    """Keep only picks whose edge was ALSO present on an earlier scan inside the window (one scan = one call).
    Fake edges (a stale or suspended price, a one-scan feed glitch) rarely survive two scans. The first sighting
    is stored and not sent. State lives on ctx (in memory), so a restart simply starts the confirmation again."""
    import time as _t
    win = persist_window(mode)
    if win <= 0 or ctx is None:
        return ranked
    now = _t.time() if now is None else now
    pend = ctx.__dict__.setdefault("pending", {})
    scan_no = ctx.__dict__.get("scan_no", 0) + 1
    ctx.scan_no = scan_no
    for k in [k for k, v in pend.items() if now - v["last_t"] > win]:
        del pend[k]
    out = []
    for p in ranked:
        k = (p.get("league"), p.get("event_id"), p.get("market"), p.get("side") or p.get("selection"), p.get("line"))
        rec = pend.get(k)
        if rec is not None and rec["last_scan"] < scan_no:
            out.append(dict(p, confirmed_scans=rec["n"] + 1))
        pend[k] = dict(last_t=now, last_scan=scan_no, n=(rec["n"] + 1) if rec else 1)
    return out


def shrink_weight() -> float:
    """Share of the Pinnacle fair probability blended into a Sim probability (0 = pure model, 1 = pure Pinnacle)."""
    return min(1.0, max(0.0, _env_float("SIM_SHRINK", 0.5)))


def sim_needs_pin_line() -> bool:
    """A Sim pick on a line Pinnacle does not price has nothing to confirm the model: dropped (default)."""
    return os.environ.get("SIM_REQUIRE_PIN_LINE", "1").strip() != "0"


def shrink_sim(cands: list, fair: dict, w: float = None, need_line: bool = None) -> list:
    """fair: {(market, line): {selection: pinnacle fair prob}}. Blend p toward it; drop picks without a line match."""
    w = shrink_weight() if w is None else w
    need = sim_needs_pin_line() if need_line is None else need_line
    out = []
    for c in cands:
        ln = None if c.get("line") is None else round(float(c["line"]), 3)
        pp = ((fair or {}).get((c.get("market"), ln)) or {}).get(c.get("selection"))
        if pp is None:
            if need:
                continue
            out.append(c)
            continue
        out.append(dict(c, p_model=c["p"], p_pin=pp, p=(1.0 - w) * c["p"] + w * pp))
    return out


# ---- Live protection (no live Pinnacle price exists, so the live fair price is the model's) ----------------
DEFAULT_MAX_EV = {"prematch": 0.20, "sim": 0.12, "live": 0.10}


def max_ev(mode: str) -> float:
    """Above this the 'edge' is far more likely a model error than value (Pinnacle-based Prematch keeps 20%)."""
    return _env_float("MAX_EV_" + mode.upper(), DEFAULT_MAX_EV.get(mode, 0.20))


def live_book_blend() -> float:
    """Share of the SportyBet de-vigged live price blended into the live model probability (0 = pure model)."""
    return min(1.0, max(0.0, _env_float("LIVE_BOOK_BLEND", 0.5)))


def live_totals_min_remaining() -> float:
    """Live totals are skipped in the last part of the game: fouling and free throws inflate late scoring."""
    return max(0.0, _env_float("LIVE_TOTALS_MIN_REMAINING", 0.20))


def live_cooldown_min() -> float:
    return max(0.0, _env_float("LIVE_GAME_COOLDOWN_MIN", 45))


def live_max_per_game() -> int:
    return max(1, _env_int("LIVE_MAX_PER_GAME", 3))


LIVE_HISTORY_MIN = 360   # how far back the per-game live history is read


def shrink_live(cands: list, w: float = None) -> list:
    """Blend the live model probability toward the de-vigged price of the same two-way market at the same book.
    A candidate whose other side is missing has no market reference and is dropped."""
    w = live_book_blend() if w is None else w
    groups = {}
    for c in cands:
        k = (c.get("event_id"), c.get("market"), None if c.get("line") is None else round(float(c["line"]), 3), c.get("book"))
        groups.setdefault(k, {})[c.get("selection")] = c.get("odds")
    out = []
    for c in cands:
        k = (c.get("event_id"), c.get("market"), None if c.get("line") is None else round(float(c["line"]), 3), c.get("book"))
        sides = groups[k]
        if len(sides) != 2 or not all(isinstance(o, (int, float)) and o > 1 for o in sides.values()):
            continue
        inv = {s: 1.0 / o for s, o in sides.items()}
        pb = inv[c["selection"]] / sum(inv.values())
        out.append(dict(c, p_model=c["p"], p_book=pb, p=(1.0 - w) * c["p"] + w * pb))
    return out


def direction(p: dict):
    """What a pick backs: a team (moneyline or spread, any line) or Over / Under."""
    kind, _ = kind_period(p)
    return ("total", p.get("selection")) if kind == "total" else ("team", p.get("side") or p.get("selection"))


def reversal_candidate(p: dict) -> bool:
    """Strong enough to be CONSIDERED as a reversal of an earlier live alert (the stance gate then applies the rest of
    the reversal rules: stable read, one per window, real score swing)."""
    return (p.get("ev") or 0) >= _env_float("STANCE_REV_MIN_EV", 0.08) - 1e-9 and \
        (p.get("p") or 0) >= _env_float("STANCE_REV_MIN_P", 0.67) - 1e-9


def live_blocked(p: dict, hist: list, now=None, allow_reversal: bool = False) -> bool:
    """True if a new live alert on this game must wait: cooldown after the last one, a cap per game, and never the
    opposite side of something already sent on the same game. allow_reversal=True lets a pick that opposes an earlier
    alert through to the stance gate, which decides whether it is a genuine reversal."""
    from datetime import datetime
    now = now or datetime.utcnow()
    hist = list(hist or [])
    if allow_reversal:
        nd = direction(p)
        if any(direction(h)[0] == nd[0] and direction(h)[1] != nd[1] for h in hist):
            return False
    if len(hist) >= live_max_per_game():
        return True
    cool = live_cooldown_min()
    for h in hist:
        t = h.get("created_at")
        if cool and t is not None and (now - t).total_seconds() < cool * 60:
            return True
        hd, nd = direction(h), direction(p)
        if hd[0] == nd[0] and hd[1] != nd[1]:
            return True
    return False
