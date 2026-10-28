"""Group 2 checks: devig, EV ranking, stake cap, Poisson, Monte Carlo, derive."""
import math
import pytest
from core import devig, ev_engine, kelly, poisson, montecarlo


def test_devig_sums_to_one_both_methods():
    for odds in ([1.91, 1.91], [1.5, 2.7], [1.2, 5.5], [2.1, 3.4, 3.6]):
        for m in ("power", "multiplicative"):
            assert abs(sum(devig.devig(odds, m)) - 1.0) < 1e-9


def test_devig_underround_and_errors():
    assert abs(sum(devig.devig([2.1, 2.1])) - 1.0) < 1e-9      # book under 100%
    for bad in ([1.0, 2.0], [2.0], [float("nan"), 2.0]):
        with pytest.raises(ValueError):
            devig.devig(bad)
    with pytest.raises(ValueError):
        devig.devig([1.9, 1.9], "nope")
    assert devig.devig([1.91, 1.95], "mult") == devig.devig([1.91, 1.95], "multiplicative")


def test_edge_boundaries():
    assert ev_engine.is_valid(0.04) and not ev_engine.is_valid(0.039)
    assert ev_engine.is_valid(0.20) and not ev_engine.is_valid(0.21)


def test_rank_best_book_top_n_and_bad_input():
    c = [dict(event_id=1, market="h2h", selection="H", p=0.55, book="a", odds=1.95),
         dict(event_id=1, market="h2h", selection="H", p=0.55, book="b", odds=1.98),
         dict(event_id=2, market="h2h", selection="H", p=0.0, book="a", odds=2.0),
         dict(event_id=3, market="h2h", selection="H", p=0.5, book="a", odds=9.0)]   # 350% = bad line
    out = ev_engine.rank(c, 5)
    assert len(out) == 1 and out[0]["book"] == "b"


def test_stake_cap_5pct_of_current_bankroll():
    assert kelly.compute_stake(100000, flat=5000, max_pct=0.05) == 5000
    assert kelly.compute_stake(60000, flat=5000, max_pct=0.05) == 3000
    assert kelly.compute_stake(123456, flat=5000, max_pct=0.05) == 5000
    assert kelly.compute_stake(1900, flat=5000, max_pct=0.05) == 0      # below one step: skip
    assert kelly.compute_stake(0, flat=5000, max_pct=0.05) == 0
    for b in range(1000, 400000, 777):
        s = kelly.compute_stake(b, flat=5000, max_pct=0.05)
        assert s <= b * 0.05 + 1e-6 and s % 100 == 0


def test_poisson_consistent():
    r = poisson.poisson_probs(112, 108, spread_line=-3.5, total_line=220.5)
    assert abs(r["home_win"] + r["away_win"] - 1) < 1e-9
    assert r["home_win"] > 0.5 and 0 < r["cover"] < r["home_win"] and 0 < r["over"] < 1
    assert r["push_spread"] == 0 and r["push_total"] == 0
    r2 = poisson.poisson_probs(112, 108, spread_line=-4, total_line=220)
    assert r2["push_spread"] > 0 and r2["push_total"] > 0
    assert poisson.poisson_probs(100, 100)["cover"] is None


def test_montecarlo_reproducible_and_sane():
    s1 = montecarlo.simulate(4, 220, 12, 17, seed=montecarlo.event_seed("e1", "h2h"))
    s2 = montecarlo.simulate(4, 220, 12, 17, seed=montecarlo.event_seed("e1", "h2h"))
    assert s1 == s2 and len(s1) == 10000
    assert abs(montecarlo.prob_home_win(s1) - 0.63) < 0.03


def test_derive_rows_prices_and_margin():
    from core.derive import derive_rows, DERIVED_MARGIN
    from config.leagues import LEAGUES
    rows = derive_rows(next(iter(LEAGUES)))
    assert rows and all(r["is_derived"] for r in rows)
    for r in rows:
        assert r["side_a_odds"] > 1 and r["side_b_odds"] > 1
        assert abs(1 / r["side_a_odds"] + 1 / r["side_b_odds"] - (1 + DERIVED_MARGIN)) < 0.01
