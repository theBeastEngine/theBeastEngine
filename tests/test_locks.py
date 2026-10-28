"""The 13 locks. Pure-Python: no DB, network or pytest plugins needed."""
import ast
import inspect
from pathlib import Path

from config.leagues import LEAGUE_KEYS
from config.markets import MARKET_KEYS, REAL_NCAAB, MATCHUP_TYPES
from config import settings as S
from core.derive import derive_rows
from core.devig import devig
from core.kelly import compute_stake
from core.modes import MODES, PREMATCH, LIVE, SIM
from core.ev_engine import rank

ROOT = Path(__file__).resolve().parents[1]


def test_lock_01_fifteen_markets():
    assert len(MARKET_KEYS) == 15 and len(set(MARKET_KEYS)) == 15


def test_lock_02_fourteen_leagues():
    assert len(LEAGUE_KEYS) == 14 and len(set(LEAGUE_KEYS)) == 14


def test_lock_03_ncaab_nine_real_markets():
    assert len(REAL_NCAAB) == 9


def test_lock_04_nine_matchup_tiers():
    assert len(MATCHUP_TYPES) == 9 and "TOP_BOT" in MATCHUP_TYPES and "BOT_TOP" in MATCHUP_TYPES


def test_lock_05_top_n_is_5_prematch():
    assert S.TOP_N == 5 and MODES[PREMATCH].top_n == 5


def test_lock_06_scan_every_120s():
    assert S.SCAN_SECONDS == 120 and MODES[LIVE].every_sec == 120


def test_lock_07_min_ev_004_and_sanity_cap():
    assert S.MIN_EV == 0.04 and S.MAX_EV_SANITY == 0.20
    c = lambda odds: [dict(event_id="e", market="h2h", selection="home", p=0.5, book="b", odds=odds)]
    assert rank(c(2.07), 5) == [] and len(rank(c(2.10), 5)) == 1 and rank(c(2.50), 5) == []


def test_lock_08_devig_sums_to_one():
    for odds in ([1.91, 1.95], [2.1, 3.4, 3.6], [1.2, 5.0]):
        for m in ("power", "mult"):
            assert abs(sum(devig(odds, m)) - 1.0) < 1e-9


def test_lock_09_gold_expects_1890_rows():
    assert S.GOLD_EXPECTED_ROWS == 1890
    assert sum(len(derive_rows(lg)) for lg in LEAGUE_KEYS) == 1890
    keys = {(r["league"], r["matchup_type"], r["market"], r["source"]) for lg in LEAGUE_KEYS for r in derive_rows(lg)}
    assert len(keys) == 1890  # UNIQUE (league, matchup_type, market, source)


def test_lock_10_modes_live3_sim3_norepeat30_mc10k_real_only():
    assert MODES[LIVE].top_n == 3 and MODES[SIM].top_n == 3
    assert MODES[LIVE].no_repeat_min == 30 and MODES[SIM].sims == 10_000
    assert all(m.real_games_only for m in MODES.values())


def test_lock_11_stake_capped_at_5pct_of_current_bankroll():
    # limit passed explicitly: never reads the setting
    assert compute_stake(200_000, flat=5000, max_pct=0.05) == 5000
    assert compute_stake(50_000, flat=5000, max_pct=0.05) == 2500
    assert compute_stake(99_999, flat=5000, max_pct=0.05) == 4900
    assert compute_stake(1_000, flat=5000, max_pct=0.05) == 0


def test_lock_12_stakes_are_multiples_of_100():
    for br in (12_345, 77_777, 150_001, 3_333_333):
        assert compute_stake(br, flat=5000, max_pct=0.05) % 100 == 0


def test_lock_13_pinnacle_gold_is_never_deleted():
    tree = ast.parse((ROOT / "storage" / "repository.py").read_text())
    names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert not [n for n in names if any(w in n for w in ("delete", "drop", "clear", "truncate", "wipe"))]
    src = (ROOT / "storage" / "repository.py").read_text()
    assert ".delete(" not in src and "DROP " not in src.upper().replace("DROPS", "")
    # whole codebase (tests excluded): no SQL delete/drop anywhere
    bad = ("delete from", "drop table", "drop_all", ".delete(")
    for f in ROOT.rglob("*.py"):
        if "tests" in f.parts or ".git" in f.parts:
            continue
        text = f.read_text().lower()
        assert not [b for b in bad if b in text], f"{f.relative_to(ROOT)} contains destructive SQL"
