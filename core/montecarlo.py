import hashlib
import math
import random
from config.settings import MC_SIMS


def event_seed(*parts) -> int:
    """Stable seed from e.g. (event_id, market). Same event -> same sims, so a pick does not
    flicker in and out of the top 3 just because the dice were re-rolled between scans."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:15], 16)


def simulate(mu_margin: float, total: float, sd_margin: float, sd_total: float,
             n: int = MC_SIMS, seed: int = None) -> list:
    """10k Monte Carlo of (margin, total) for simulation mode. Returns list of (margin, total)."""
    if n < 1:
        raise ValueError("n must be >= 1")
    if not (sd_margin > 0 and sd_total > 0):
        raise ValueError("sd_margin and sd_total must be > 0")
    rng = random.Random(seed)
    return [(rng.gauss(mu_margin, sd_margin), rng.gauss(total, sd_total)) for _ in range(n)]


def prob_home_win(sims) -> float:
    return sum(1 for m, _ in sims if m > 0) / len(sims)


def prob_cover(sims, home_line: float) -> float:
    return sum(1 for m, _ in sims if m + home_line > 0) / len(sims)


def prob_over(sims, line: float) -> float:
    return sum(1 for _, t in sims if t > line) / len(sims)


def std_err(p: float, n: int) -> float:
    """Sampling error of a simulated probability. At n=10,000 and p~0.5 it is ~0.005,
    i.e. about +/-1% of EV on even odds: keep that in mind next to MIN_EV = 4%."""
    return math.sqrt(max(p * (1 - p), 0.0) / n)
