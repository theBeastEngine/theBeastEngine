"""Group A: scraper live-row safety + Sofascore league mapping."""
import asyncio
import types

from core.scores import sofascore as sf
from core.scores.sofascore import league_of
from services import scraper
from services.scraper import same_team


# ---------- scraper ----------
def test_same_team_variants_and_rivals():
    assert same_team("LA Lakers", "Los Angeles Lakers")
    assert same_team("Philadelphia 76ers", "Philadelphia Sixers")
    assert same_team("FC Barcelona", "Barcelona")
    assert same_team("Anadolu Efes", "Anadolu Efes Istanbul")
    assert not same_team("Los Angeles Lakers", "Los Angeles Clippers")
    assert not same_team("Brooklyn Nets", "New York Knicks")
    assert not same_team("", "Boston Celtics")


def _fake_adapter(rows):
    async def fetch_native_live(league, home, away):
        return rows
    return types.SimpleNamespace(fetch_native_live=fetch_native_live, __name__="fake")


def _row(home, away, side="home", odds=1.9):
    return dict(match_id="x:1", market="ML", side=side, odds=odds, line=None, book="fake", home=home, away=away)


def test_live_rows_from_other_games_are_dropped(monkeypatch):
    rows = [_row("Boston Celtics", "New York Knicks"),
            _row("Miami Heat", "Orlando Magic"),                  # other game
            _row("New York Knicks", "Boston Celtics")]            # reversed orientation
    monkeypatch.setattr(scraper, "SOFT_BOOKS", ("fake",))
    monkeypatch.setattr(scraper, "get_adapter", lambda b: _fake_adapter(rows))
    out = asyncio.run(scraper.scrape_live_soft("nba", "Boston Celtics", "New York Knicks"))
    assert len(out) == 1 and out[0]["home"] == "Boston Celtics" and out[0]["league"] == "nba"


def test_live_rows_without_own_names_are_kept(monkeypatch):
    r = _row(None, None)
    r.pop("home"), r.pop("away")
    monkeypatch.setattr(scraper, "SOFT_BOOKS", ("fake",))
    monkeypatch.setattr(scraper, "get_adapter", lambda b: _fake_adapter([r]))
    out = asyncio.run(scraper.scrape_live_soft("nba", "A Team", "B Team"))
    assert len(out) == 1 and out[0]["away"] == "B Team"


def test_scraper_failure_log_names_the_book(monkeypatch):
    seen = []
    monkeypatch.setattr(scraper, "log", types.SimpleNamespace(warning=lambda *a, **k: seen.append(a), info=lambda *a, **k: None))

    async def boom(*a):
        raise RuntimeError("feed down")
    boom.__module__ = "core.bookmakers.sportybet"
    assert asyncio.run(scraper._run(boom, "nba")) == []
    assert "sportybet" in str(seen[0]) and "feed down" in str(seen[0])


# ---------- sofascore ----------
def test_league_of_unchanged_without_category():
    assert league_of("NBA") == "nba"
    assert league_of("NBA G League") == "g_league"
    assert league_of("EuroLeague") == "euroleague"
    assert league_of("WNBA") is None
    assert league_of("LNB") == "lnb"


def test_league_of_uses_country():
    assert league_of("LNB", "France") == "lnb"
    assert league_of("LNB", "Chile") is None
    assert league_of("Basketbol S\u00fcper Ligi", "T\u00fcrkiye") == "bsl"
    assert league_of("Superliga", "Serbia") is None
    assert league_of("Liga Endesa", "Spain") == "acb"
    assert league_of("Liga Femenina", "Spain") is None
    assert league_of("Basketball Bundesliga", "Germany") == "bbl"
    assert league_of("2. Basketball Bundesliga ProA", "Germany") is None
    assert league_of("B.League", "Japan") == "japan_b1"
    assert league_of("B.League 2", "Japan") is None
    assert league_of("NBA Summer League", "USA") is None


def _ev(tn="NBA", cat="USA", ut="NBA"):
    return {"id": 7, "tournament": {"name": tn, "category": {"name": cat}, "uniqueTournament": {"name": ut}},
            "homeTeam": {"name": "Boston Celtics"}, "awayTeam": {"name": "New York Knicks"},
            "homeScore": {"current": 31}, "awayScore": {"current": 28},
            "status": {"description": "2nd quarter"}, "time": {"currentPeriodStartTimestamp": 1000}}


def test_parse_event_ok_and_null_unique_tournament():
    g = sf.parse_event(_ev(), now=1300)
    assert g["league"] == "nba" and g["period"] == 2 and g["elapsed"] == 300 and g["home_score"] == 31
    e = _ev()
    e["tournament"]["uniqueTournament"] = None          # used to raise AttributeError and drop the game
    assert sf.parse_event(e, now=1300)["league"] == "nba"
    assert sf.parse_event(_ev(tn="LNB", cat="Chile", ut="LNB"), now=1300)["league"] is None


def test_philippine_ncaa_is_not_us_college():
    from core.bookmakers._base import league_matches, infer_league
    assert not league_matches("Philippines NCAA, Seniors", "ncaab")
    assert infer_league("Philippines NCAA, Seniors") is None
    assert league_matches("USA NCAA", "ncaab")
    assert league_of("NCAA, Seniors", "Philippines") is None
    assert league_of("NCAA", "USA") == "ncaab"
