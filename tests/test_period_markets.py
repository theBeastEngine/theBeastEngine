"""Quarter and first-half bets: SportyBet parsing, OddsPapi period ladders, Pinnacle fair prices, labels."""
from datetime import datetime, timedelta, timezone

from config.markets import BY_KEY
from core import gates
from core.bookmakers import sportybet as sb
from core.bookmakers.soft_feed import rows_to_events
from core.pinref import fair_probs, _TWO_WAY
from services import oddspapi as op


def _out(i, desc, odds):
    return {"id": str(i), "desc": desc, "odds": str(odds), "isActive": 1}


def _mk(mid, spec, outs):
    return {"id": mid, "specifier": spec, "status": 0, "outcomes": outs}


def _payload(markets):
    return {"data": {"tournaments": [{"categoryName": "Spain", "name": "ACB", "events": [{
        "eventId": "sr:match:1", "homeTeamName": "Real Madrid", "awayTeamName": "Barcelona",
        "estimateStartTime": 4102444800000, "markets": markets}]}]}}


FULL = [_mk(219, None, [_out(4, "Home", 1.5), _out(5, "Away", 2.6)]),
        _mk(223, "hcp=-3.5", [_out(1714, "Home (-3.5)", 1.9), _out(1715, "Away (+3.5)", 1.9)]),
        _mk(225, "total=160.5", [_out(12, "Over 160.5", 1.9), _out(13, "Under 160.5", 1.9)])]


def test_sportybet_parses_quarter_and_half_rows():
    mk = FULL + [
        _mk(236, "total=40.5|quarternr=1", [_out(12, "Over 40.5", 1.85), _out(13, "Under 40.5", 1.95)]),
        _mk(236, "total=41.5|quarternr=1", [_out(12, "Over 41.5", 2.2), _out(13, "Under 41.5", 1.6)]),
        _mk(236, "total=39.5|quarternr=3", [_out(12, "Over 39.5", 1.9), _out(13, "Under 39.5", 1.9)]),
        _mk(303, "hcp=-0.5|quarternr=2", [_out(1714, "Home (-0.5)", 1.8), _out(1715, "Away (+0.5)", 2.0)]),
        _mk(68, "total=80.5", [_out(12, "Over 80.5", 1.9), _out(13, "Under 80.5", 1.9)]),
        _mk(66, "hcp=-2.5", [_out(1714, "Home (-2.5)", 1.9), _out(1715, "Away (+2.5)", 1.9)]),
        _mk(235, "quarternr=4", [_out(1, "Home", 2.0), _out(2, "Draw", 8.0), _out(3, "Away", 2.0)])]
    rows = sb.parse_events(_payload(mk))
    names = {r["market"] for r in rows}
    assert {"Spread", "Total", "ML", "Total_q1", "Total_q3", "Spread_q2", "Total_h1", "Spread_h1"} <= names
    q1 = {r["side"]: r["line"] for r in rows if r["market"] == "Total_q1"}
    assert q1 == {"over": 40.5, "under": 40.5}      # the balanced line wins (40.5), not 41.5
    assert not any(r["market"].endswith("_q4") for r in rows)   # the three-way quarter winner is ignored
    away = [r for r in rows if r["market"] == "Spread_q2" and r["side"] == "away"][0]
    assert away["line"] == 0.5


def test_handicap_with_other_sign_convention_is_dropped():
    bad = [_mk(303, "hcp=-0.5|quarternr=2", [_out(1714, "Home (+0.5)", 1.8), _out(1715, "Away (-0.5)", 2.0)])]
    rows = sb.parse_events(_payload(FULL + bad))
    assert not any(r["market"] == "Spread_q2" for r in rows)


def test_sides_are_read_by_description_not_position():
    swapped = [_mk(236, "total=40.5|quarternr=1", [_out(13, "Under 40.5", 1.95), _out(12, "Over 40.5", 1.85)])]
    rows = sb.parse_events(_payload(FULL + swapped))
    d = {r["side"]: r["odds"] for r in rows if r["market"] == "Total_q1"}
    assert d == {"over": 1.85, "under": 1.95}


def test_soft_feed_canonical_names_and_home_line():
    rows = sb.parse_events(_payload(FULL + [
        _mk(303, "hcp=-0.5|quarternr=2", [_out(1714, "Home (-0.5)", 1.8), _out(1715, "Away (+0.5)", 2.0)])]))
    ev = rows_to_events(rows, "acb")[0]
    sp = ev["markets"]["spreads_q2"]
    assert {o["selection"]: o["line"] for o in sp} == {"home": -0.5, "away": -0.5}   # both carry the HOME line


def test_market_registry_knows_all_periods():
    for k, kind, per in (("totals_q3", "total", "q3"), ("totals_q4", "total", "q4"), ("totals_h1", "total", "h1"),
                         ("spreads_q1", "spread", "q1")):
        assert (BY_KEY[k].kind, BY_KEY[k].period) == (kind, per)
        assert gates.kind_period({"market": k}) == (kind, per)
    assert "totals_q3" in _TWO_WAY and "spreads_h1" in _TWO_WAY


def _cat(mid, mtype, period, name, hcap, outs):
    return dict(marketId=mid, marketName=name, marketType=mtype, period=period, handicap=hcap,
                outcomes=[dict(outcomeId=i, outcomeName=n) for i, n in outs])


def test_period_code_and_first_quarter_name_not_skipped():
    q = _cat(1, "totals", "p1", "First Quarter Total 40.5", 40.5, [(1, "Over"), (2, "Under")])
    assert op.period_code(q) == "q1" and op.classify(q)[0] == "totals"
    h = _cat(2, "spreads", "p1+p2", "1st Half Spread -2.5", -2.5, [(3, "1"), (4, "2")])
    assert op.period_code(h) == "h1"
    assert op.period_code(_cat(3, "totals", "result", "Total", 160.5, [])) == "full"
    assert op.period_code(q, n_periods=2) is None      # a two-half league's p1 is a half, not a quarter
    assert op.period_code(_cat(4, "totals", "p5", "x", 1, [])) is None


def _pr(price):
    return dict(outcomes={}) if price is None else None


def test_normalize_keeps_quarter_and_half_ladders_with_sign_fix():
    cat = {
        101: _cat(101, "moneyline", "result", "Moneyline", 0, [(101, "1"), (102, "2")]),
        1001: _cat(1001, "spreads", "result", "Spread -6.5", -6.5, [(1001, "1"), (1002, "2")]),
        2001: _cat(2001, "spreads", "p1", "Q1 Spread -1.5", -1.5, [(2001, "1"), (2002, "2")]),
        2101: _cat(2101, "totals", "p2", "Q2 Total 40.5", 40.5, [(2101, "Over"), (2102, "Under")]),
        2201: _cat(2201, "totals", "p1+p2", "1st Half Total 80.5", 80.5, [(2201, "Over"), (2202, "Under")]),
        2301: _cat(2301, "moneyline", "p1", "Q1 Moneyline", 0, [(2301, "1"), (2302, "2")]),
    }

    def m(prices):
        return dict(outcomes={str(o): dict(players={"0": dict(active=True, price=p)}) for o, p in prices.items()})
    fx = dict(fixtureId="f9", participant1Id=1, participant2Id=2, startTime="2099-01-01T19:00:00.000Z",
              bookmakerOdds={"pinnacle": dict(markets={
                  "101": m({101: 3.2, 102: 1.35}),            # AWAY favourite
                  "1001": m({1001: 1.95, 1002: 1.95}),         # feed says home -6.5 -> contradicts: flip
                  "2001": m({2001: 1.9, 2002: 1.9}),
                  "2101": m({2101: 1.9, 2102: 1.9}),
                  "2201": m({2201: 1.9, 2202: 1.9}),
                  "2301": m({2301: 2.0, 2302: 1.8})})})
    ev = op.normalize([fx], {"1": "A", "2": "B"}, cat, "acb", {})[0]
    assert set(ev["markets"]) == {"h2h", "spreads", "spreads_q1", "totals_q2", "totals_h1"}   # no quarter moneyline
    assert {o["line"] for o in ev["markets"]["spreads"]} == {6.5}
    assert {o["line"] for o in ev["markets"]["spreads_q1"]} == {1.5}                          # flipped with the game
    assert {o["line"] for o in ev["markets"]["totals_q2"]} == {40.5}


def test_fair_probs_and_candidate_for_a_quarter_total(tmp_path):
    from config.settings import PINNACLE
    from core.pinref import PinRef
    c = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=24)

    def tot(book, over, under):
        return [dict(book=book, selection="over", line=40.5, odds=over), dict(book=book, selection="under", line=40.5, odds=under)]
    pin = dict(event_id="p1", league="nba", home="Los Angeles Lakers", away="Boston Celtics", commence=c,
               markets={"totals_q1": tot(PINNACLE, 1.80, 2.05)})
    f = fair_probs(pin)[("totals_q1", 40.5)]
    assert abs(f["over"] + f["under"] - 1) < 1e-9 and f["over"] > f["under"]
    soft = dict(event_id="s1", league="nba", home="LA Lakers", away="Boston Celtics", commence=c,
                markets={"totals_q1": tot("sportybet", 2.05, 1.75)})
    ref = PinRef(api=None, path=str(tmp_path / "pin.json"))
    ref.data["leagues"]["nba"] = dict(fetched_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                                      events=[dict(pin, commence=c.isoformat())])
    out = ref.candidates(soft)
    assert out and out[0]["market"] == "totals_q1" and out[0]["selection"] == "over"
    assert gates.kind_period(out[0]) == ("total", "q1")


def test_label_shows_the_quarter():
    from core.telegram_notify import _mlabel
    assert _mlabel({"market": "totals_q3"}) == "Total (Q3)"
    assert _mlabel({"market": "spreads_h1"}) == "Spread (1st half)"
