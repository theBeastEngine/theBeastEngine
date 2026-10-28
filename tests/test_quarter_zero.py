from core import quarters


def test_quarter_clock_at_zero_is_a_break_not_a_crash():
    for lg, n, L in (("nba", 4, 720), ("euroleague", 4, 600), ("kbl", 4, 600)):
        for per, ph in ((1, "B1"), (2, "HT"), (3, "B3")):
            o = quarters.outlook(lg, per, L, 30, 28, 2.0, 220.0, n, L)
            assert o and o["halftime"] and o["break_after"] == per
            assert o["quarters"][0]["label"].startswith("Q%d" % (per + 1))
            assert quarters.phase_of(o) == ph


def test_last_quarter_at_zero_returns_none():
    assert quarters.outlook("nba", 4, 720, 100, 98, 1.0, 220.0, 4, 720) is None


def test_zero_length_period_is_safe():
    quarters.outlook("x", 1, 0, 0, 0, 0.0, 200.0, 4, 0)


def test_normal_case_unchanged():
    o = quarters.outlook("nba", 1, 360, 25, 22, 2.0, 220.0, 4, 720)
    assert [q["label"] for q in o["quarters"]] == ["Q1 (rest)", "Q2", "Q3", "Q4"]
    assert not o["halftime"]


def test_break_score_gives_the_exact_rows_in_the_next_message():
    o = quarters.outlook("kbl", 2, 5.0, 24, 21, 4.0, 160.0, 4, 600)
    o.update(home="Wonju DB", away="Samsung", n=4, clock="9:55")
    msg = quarters.phase_message(o, q1_total=45)
    for row in ("Q1 Break", "Earlier Q2", "Rest of Q2", "1st Half"):
        assert row in msg
    assert "Played so far" not in msg
