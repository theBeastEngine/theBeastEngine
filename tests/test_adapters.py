# tests/test_adapters.py  -- offline parser tests (fixtures are hand-built, NOT captured from the live sites)
import pytest

from core.bookmakers import clean_rows, get_adapter
from core.bookmakers import _onex, bet9ja, sportybet
from core.bookmakers._base import league_matches, most_balanced, pair_candidates

SOFT8 = ["sportybet", "bet9ja", "betano", "1xbet", "betking", "msport", "nairabet", "paripesa"]


@pytest.mark.parametrize("book", SOFT8)
def test_adapter_loads_and_has_contract(book):
    a = get_adapter(book)
    assert a is not None and a.BOOK == book and callable(a.fetch_odds)


def test_league_rules():
    assert league_matches("USA NBA", "nba") and not league_matches("USA WNBA", "nba")
    assert league_matches("USA NBA G League", "g_league") and not league_matches("USA NBA G League", "nba")
    assert league_matches("Spain Liga ACB", "acb") and not league_matches("Spain Liga ACB Women", "acb")
    assert not league_matches("USA NCAA Women", "ncaab")


def test_main_line_is_most_balanced():
    c = pair_candidates([(-3.5, 1.9), (-9.5, 1.5)], [(3.5, 1.9), (9.5, 2.6)], negate=True)
    assert most_balanced(c)[0] == -3.5


def _one(rows, market, side):
    return next(r for r in rows if r["market"] == market and r["side"] == side)


def test_onex_parse():
    ev = {"I": 1, "L": "USA. NBA", "O1": "Lakers", "O2": "Celtics", "S": 1790000000,
          "E": [{"G": 1, "T": 1, "C": 1.9}, {"G": 1, "T": 3, "C": 1.95},
                {"G": 2, "T": 7, "C": 1.9, "P": -2.5}, {"G": 2, "T": 8, "C": 1.9, "P": 2.5},
                {"G": 17, "T": 9, "C": 1.9, "P": 220.5}, {"G": 17, "T": 10, "C": 1.9, "P": 220.5}]}
    rows = clean_rows(_onex.parse_value([ev], "1xbet", "nba"))
    assert len(rows) == 6
    assert _one(rows, "Spread", "away")["line"] == 2.5 and _one(rows, "Total", "over")["line"] == 220.5
    assert _onex.parse_value([ev], "1xbet", "euroleague") == []


def test_sportybet_parse():
    payload = {"data": {"tournaments": [{"categoryName": "USA", "name": "NBA", "events": [{
        "eventId": "sr:match:1", "homeTeamName": "Lakers", "awayTeamName": "Celtics",
        "estimateStartTime": 1790000000000, "markets": [
            {"id": "219", "outcomes": [{"odds": "1.9"}, {"odds": "1.95"}]},
            {"id": "223", "specifier": "hcp=-2.5", "outcomes": [{"odds": "1.9"}, {"odds": "1.9"}]},
            {"id": "225", "specifier": "total=220.5",
             "outcomes": [{"desc": "Over 220.5", "odds": "1.9"}, {"desc": "Under 220.5", "odds": "1.9"}]}]}]}]}}
    rows = clean_rows(sportybet.parse_events(payload, "nba"))
    assert len(rows) == 6 and _one(rows, "Spread", "home")["line"] == -2.5


def test_bet9ja_parse():
    payload = {"D": {"E": [{"ID": 7, "DS": "Lakers - Celtics", "STARTDATE": "2026-10-20 02:00:00",
                            "O": {"S_12_1": "1.9", "S_12_2": "1.95", "S_AH@-2.5_1": "1.9", "S_AH@2.5_2": "1.9",
                                  "S_OU@220.5_O": "1.9", "S_OU@220.5_U": "1.9"}}]}}
    rows = clean_rows(bet9ja.parse_events(payload, "nba"))
    assert len(rows) == 6 and _one(rows, "Spread", "away")["line"] == 2.5


def test_rows_reach_engine_market_names():
    from core.bookmakers.soft_feed import rows_to_events
    payload = {"data": {"tournaments": [{"categoryName": "USA", "name": "NBA", "events": [{
        "eventId": "sr:match:1", "homeTeamName": "Lakers", "awayTeamName": "Celtics",
        "estimateStartTime": 1790000000000, "markets": [
            {"id": "219", "outcomes": [{"odds": "1.9"}, {"odds": "1.95"}]},
            {"id": "223", "specifier": "hcp=-2.5", "outcomes": [{"odds": "1.9"}, {"odds": "1.9"}]},
            {"id": "225", "specifier": "total=220.5",
             "outcomes": [{"desc": "Over 220.5", "odds": "1.9"}, {"desc": "Under 220.5", "odds": "1.9"}]}]}]}]}}
    evs = rows_to_events(sportybet.parse_events(payload, "nba"), "nba")
    assert len(evs) == 1 and set(evs[0]["markets"]) == {"h2h", "spreads", "totals"}


def test_spread_lines_become_home_line():
    from core.bookmakers.soft_feed import rows_to_events
    base = dict(match_id="k:1", book="k", home="Lakers", away="Celtics", market="Spread", odds=1.9)
    rows = [dict(base, side="home", line=-2.5), dict(base, side="away", line=2.5)]
    sp = rows_to_events(rows, "nba")[0]["markets"]["spreads"]
    assert [x["line"] for x in sp] == [-2.5, -2.5]
