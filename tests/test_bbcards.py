from core import bbcards


def _rows(spread_ev=0.01):
    r = lambda m, s, ln, o, f, ev: dict(market=m, selection=s, line=ln, odds=o, p=1 / f, fair_odds=f, ev=ev)
    return [r("h2h", "home", None, 1.50, 1.45, 0.034), r("h2h", "away", None, 2.60, 2.95, -0.119),
            r("spreads", "home", -5.5, 1.90, 1.92, -0.01), r("spreads", "away", -5.5, 1.90, 1.92, -0.01),
            r("spreads", "home", -7.5, 2.10, 2.08, spread_ev), r("spreads", "away", -7.5, 1.70, 1.74, -0.023),
            r("totals", "over", 221.5, 1.85, 1.90, -0.026), r("totals", "under", 221.5, 1.85, 1.92, -0.036)]


def test_table_has_one_closest_line_per_market_in_the_tennis_layout():
    tb = bbcards.compare_table(_rows(), "Los Angeles Lakers", "Los Angeles Clippers")
    assert [h for h, _ in tb] == ["MONEYLINE", "SPREAD (closest line)", "TOTAL POINTS (closest line)"]
    sp = dict(tb)["SPREAD (closest line)"]
    assert [round(x[3], 3) for x in sp] == [0.01, -0.023] and sp[0][0].endswith("-7.5") and sp[1][0].endswith("+7.5")   # best-edge line, both sides
    txt = bbcards.table_text(tb)
    rows = [r for blk in txt if blk.startswith("<pre>") for r in blk[5:-6].split("\n")]
    assert all(len(r) <= 28 for r in rows) and len(rows) == 6
    assert rows[0].startswith("Lakers") and rows[1].startswith("Clippers")           # nickname, so the two LA teams differ
    assert rows[0].endswith("+3.4%") and rows[1].endswith("-11.9%")


def test_plain_result_says_how_far_the_closest_market_is_from_a_bet():
    s = bbcards.plain_result(_rows(), "Home FC", "Away FC", 0.04)
    assert s.startswith("NO BET. Closest to a bet: Home FC to win (SportyBet 1.50, Pinnacle fair 1.45)")
    assert "Edge +3.4%" in s and "0.6 points short of the 4% needed" in s
    assert bbcards.plain_result([], "A", "B", 0.04) == ""


def test_chances_and_read_come_from_pinnacle_fair_prices():
    tb = bbcards.compare_table(_rows(), "Melbourne United", "Illawarra Hawks")
    ch = bbcards.chances(tb)
    assert ch[0].startswith("Moneyline: ") and "%" in ch[0] and len(ch) == 3
    rd = bbcards.match_read(tb)
    assert rd.startswith("Melbourne") and "favourite" in rd and "Pinnacle's total is 221.5 points" in rd and rd.endswith("(Pinnacle's view, not a tip.)")


def test_extra_markets_lists_quarter_and_half_lines_pinnacle_does_not_price():
    sb = lambda: [dict(book="sportybet", selection="over", line=50.5, odds=1.9)]
    pn = lambda: [dict(book="pinnacle", selection="over", line=50.5, odds=1.9)]
    ev = dict(markets={"totals": sb(), "totals_q1": sb(), "spreads_h1": sb(), "totals_h1": sb()})
    pin = dict(markets={"totals": pn(), "totals_h1": pn()})
    assert bbcards.extra_markets(ev, pin) == ["Total (Q1)", "Spread (1st half)"]
