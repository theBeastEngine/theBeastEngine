from datetime import datetime, timedelta

from core import calib, restflag


def _rows(n, p, odds, win_rate):
    wins = int(n * win_rate)
    return [{"p": p, "odds": odds, "won": i < wins} for i in range(n)]


def test_ev_of():
    assert abs(calib.ev_of(0.55, 1.95) - 0.0725) < 1e-9


def test_ignores_open_and_bad_rows():
    rows = [
        {"p": 0.5, "odds": 2.0, "won": None},
        {"p": 1.5, "odds": 2.0, "won": True},
        {"p": 0.5, "odds": 1.0, "won": True},
        {"odds": 2.0, "won": True},
        {"p": 0.5, "odds": 2.0, "won": True},
    ]
    assert calib.calibration(rows)["overall"]["n"] == 1


def test_overall_and_roi():
    cal = calib.calibration(_rows(100, 0.55, 2.0, 0.50))
    o = cal["overall"]
    assert o["n"] == 100
    assert abs(o["pred"] - 0.55) < 1e-9 and abs(o["actual"] - 0.50) < 1e-9
    assert abs(o["roi"] - 0.0) < 1e-9
    assert cal["enough"]


def test_bucket_assignment():
    rows = [
        {"p": 0.52, "odds": 2.0, "won": True},   # EV +4%
        {"p": 0.60, "odds": 2.0, "won": False},  # EV +20%
    ]
    b = {x["label"]: x["n"] for x in calib.calibration(rows)["buckets"]}
    assert b["4%-6%"] == 1 and b["15% and up"] == 1


def test_clv():
    rows = [{"p": 0.5, "odds": 2.0, "won": True, "close_odds": 1.8}]
    cal = calib.calibration(rows)
    assert abs(cal["clv_avg"] - (2.0 / 1.8 - 1)) < 1e-9 and cal["clv_n"] == 1


def test_report_texts():
    assert "no settled bets" in calib.format_report(calib.calibration([]))
    small = calib.format_report(calib.calibration(_rows(10, 0.55, 2.0, 0.5)))
    assert "too few" in small
    bad = calib.format_report(calib.calibration(_rows(100, 0.60, 2.0, 0.40)))
    assert "too optimistic" in bad and "<" not in bad and ">" not in bad
    good = calib.format_report(calib.calibration(_rows(100, 0.55, 2.0, 0.55)))
    assert "look honest" in good


T = datetime(2026, 10, 20, 19, 0)


def test_b2b():
    i = restflag.rest_info([T - timedelta(hours=22)], T)
    assert i["b2b"] and i["rest_days"] == 0 and not i["third_in_four"]


def test_third_in_four_and_none():
    i = restflag.rest_info([T - timedelta(days=3), T - timedelta(days=1, hours=22)], T)
    assert i["third_in_four"] and not i["b2b"]
    assert restflag.rest_info([], T)["rest_days"] is None
    assert restflag.rest_info([T + timedelta(days=1)], T)["games_last4"] == 0


def test_flag_text():
    home = [T - timedelta(days=3)]
    away = [T - timedelta(hours=20)]
    s = restflag.rest_flag(home, away, T, "Kings", "Lakers")
    assert "Lakers on a back-to-back" in s and "Kings has 3 more rest days" in s
    assert restflag.rest_flag([T - timedelta(days=2)], [T - timedelta(days=2)], T, "A", "B") is None


def test_rows_from_picks():
    picks = [
        {"status": "won", "fair_odds": 2.0, "book_odds": 2.1, "took_odds": 2.05, "mode": "prematch"},
        {"status": "lost", "fair_odds": 1.8, "book_odds": 1.9, "took_odds": None, "mode": "live"},
        {"status": "placed", "fair_odds": 2.0, "book_odds": 2.1},
        {"status": "won", "fair_odds": None, "book_odds": 2.1},
    ]
    rows = calib.rows_from_picks(picks, closing=lambda p: 0.5 if p["mode"] == "prematch" else None)
    assert len(rows) == 2
    assert abs(rows[0]["p"] - 0.5) < 1e-9 and rows[0]["odds"] == 2.05 and rows[0]["won"]
    assert abs(rows[0]["close_odds"] - 2.0) < 1e-9
    assert rows[1]["odds"] == 1.9 and not rows[1]["won"] and "close_odds" not in rows[1]
    cal = calib.calibration(rows)
    assert abs(cal["clv_avg"] - (2.05 / 2.0 - 1)) < 1e-9
