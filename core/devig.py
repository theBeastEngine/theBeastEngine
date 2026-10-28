"""Remove the bookmaker margin. Every method returns probabilities that sum to 1."""
import math

METHODS = ("power", "multiplicative")
_ALIASES = {"power": "power", "multiplicative": "multiplicative", "mult": "multiplicative",
            "mul": "multiplicative", "proportional": "multiplicative", "basic": "multiplicative"}


def _check(odds):
    odds = list(odds)
    if len(odds) < 2:
        raise ValueError("need at least 2 outcomes to remove the margin")
    for o in odds:
        if not isinstance(o, (int, float)) or not math.isfinite(o) or o <= 1.0:
            raise ValueError("decimal odds must be finite and > 1")
    return odds


def implied(odds):
    return [1.0 / o for o in odds]


def multiplicative(odds):
    q = implied(odds)
    s = sum(q)
    return [x / s for x in q]


def power(odds, tol=1e-12):
    """Power method: find k with sum(q_i ** k) == 1 (better for favourite/longshot bias).
    Works for overround (k > 1) and for under-round / arb books (k < 1)."""
    q = implied(odds)
    if abs(sum(q) - 1.0) < tol:
        return q
    lo, hi = 0.0, 50.0          # sum(q**k) is strictly decreasing in k (every q < 1)
    for _ in range(200):
        k = (lo + hi) / 2
        if sum(x ** k for x in q) > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    p = [x ** k for x in q]
    s = sum(p)
    return [x / s for x in p]


def devig(odds, method="power"):
    odds = _check(odds)
    m = _ALIASES.get(method)
    if m is None:
        raise ValueError(f"unknown devig method {method!r}; use one of {sorted(_ALIASES)}")
    return power(odds) if m == "power" else multiplicative(odds)


def fair_odds(odds, method="power"):
    return [1.0 / p for p in devig(odds, method)]


def margin(odds):
    return sum(implied(_check(odds))) - 1.0
