import math
from config.leagues import LEAGUES
from core.derive import ncdf

FORMATS = {"ncaab": (2, 1200), "nba": (4, 720), "g_league": (4, 720), "cba": (4, 720), "pba": (4, 720)}
DEFAULT_FORMAT = (4, 600)
TOTAL_SD_MULT = 1.4  # total-points sd relative to margin sd (NBA: ~17-18 vs ~12-13); was 1.2 = overconfident
MIN_REMAINING = 0.08  # skip the last ~8% of the game: lines are noise there


def fmt(league):
    return FORMATS.get(league, DEFAULT_FORMAT)


def frac_remaining(league, period, elapsed):
    n, L = fmt(league)
    if period < 1 or period > n:
        return 0.0
    elapsed = min(max(elapsed, 0.0), L)
    return ((n - period) * L + (L - elapsed)) / (n * L)


def pregame_params(idx, league, mt):
    """(expected home margin, expected total, margin sd) from gold rows, league priors as fallback."""
    cfg = LEAGUES[league]
    mu, T = cfg["home_adv"], cfg["avg_total"]
    r = idx.get((mt, "spreads"))
    if r and r.get("line") is not None:
        mu = -r["line"]
    r = idx.get((mt, "totals"))
    if r and r.get("line") is not None:
        T = r["line"]
    return mu, T, cfg["std_margin"]


def live_prob(market, selection, line, rem, diff, cur_total, mu, T, sd):
    """Win prob of a selection given the live state. Live spread/total lines apply to the FINAL score."""
    if rem <= 0:
        return None
    sdr = sd * math.sqrt(rem)
    mean = diff + mu * rem
    if market == "h2h":
        p_home = ncdf(mean / sdr)
        return p_home if selection == "home" else 1 - p_home
    if line is None:
        return None
    if market == "spreads":
        p_home = ncdf((mean + line) / sdr)
        return p_home if selection == "home" else 1 - p_home
    if market == "totals":
        # CLAUDE FIX: project from the pace the game is REALLY playing, not from the pregame total alone.
        # blended full-game pace = (1-f)*T + f*(cur_total/f) = rem*T + cur_total, where f = share already played.
        # (Dyip-Tropang: 176 pts with 6:19 left projected ~202; the old formula said ~190 and called Under 193.5 a 64% bet.)
        blended = rem * T + cur_total
        proj = cur_total + rem * blended
        p_over = 1 - ncdf((line - proj) / (sd * TOTAL_SD_MULT * math.sqrt(rem)))
        return p_over if selection == "over" else 1 - p_over
    return None
