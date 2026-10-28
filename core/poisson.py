import math


def _pmf_window(mean: float):
    """Poisson pmf over mean +/- 8 sd (log-space, no underflow). Returns (lo, [p_lo .. p_hi])."""
    sd = math.sqrt(mean)
    lo = max(0, int(mean - 8 * sd - 5))
    hi = int(mean + 8 * sd + 5)
    lm = math.log(mean)
    return lo, [math.exp(k * lm - mean - math.lgamma(k + 1)) for k in range(lo, hi + 1)]


def poisson_probs(mu_home: float, mu_away: float, spread_line: float = None, total_line: float = None) -> dict:
    """Independent-Poisson team scores. spread_line is the HOME line (e.g. -5.5).
    home_win/away_win: ties (OT) renormalised away. cover/over: probability of a strict win
    on that line; push_spread/push_total hold the integer-line push mass (0 on half lines).
    cover and over are None when the matching line is not given (never a fake 0.0)."""
    if not (mu_home > 0 and mu_away > 0):
        raise ValueError("mu_home and mu_away must be > 0")
    lh, ph = _pmf_window(mu_home)
    la, pa = _pmf_window(mu_away)
    mass = win = loss = cover = push_s = over = push_t = 0.0
    for i, x in enumerate(ph):
        h = lh + i
        for j, y in enumerate(pa):
            a = la + j
            p = x * y
            mass += p
            d = h - a
            if d > 0:
                win += p
            elif d < 0:
                loss += p
            if spread_line is not None:
                v = d + spread_line
                if v > 1e-9:
                    cover += p
                elif abs(v) <= 1e-9:
                    push_s += p
            if total_line is not None:
                t = h + a - total_line
                if t > 1e-9:
                    over += p
                elif abs(t) <= 1e-9:
                    push_t += p
    s = win + loss
    return {
        "home_win": win / s, "away_win": loss / s,
        "cover": cover / mass if spread_line is not None else None,
        "push_spread": push_s / mass if spread_line is not None else None,
        "over": over / mass if total_line is not None else None,
        "push_total": push_t / mass if total_line is not None else None,
    }
