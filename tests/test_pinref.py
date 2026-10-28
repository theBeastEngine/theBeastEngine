from datetime import datetime, timedelta, timezone

from config.settings import PINNACLE
from core.pinref import PinRef, COST_PER_REFRESH


def _iso(delta_h):
    return (datetime.now(timezone.utc) + timedelta(hours=delta_h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _raw(home, away, hours=24, h_odds=1.80, a_odds=2.05):
    return {"id": f"{home}-{away}", "home_team": home, "away_team": away, "commence_time": _iso(hours),
            "bookmakers": [{"key": PINNACLE, "markets": [
                {"key": "h2h", "outcomes": [{"name": home, "price": h_odds}, {"name": away, "price": a_odds}]},
                {"key": "totals", "outcomes": [{"name": "Over", "price": 1.9, "point": 220.5},
                                               {"name": "Under", "price": 1.9, "point": 220.5}]}]}]}


class FakeAPI:
    def __init__(self, raw):
        self.raw, self.calls = raw, 0

    def fetch(self, key, markets=None, books=None, regions="eu"):
        self.calls += 1
        return self.raw


def _soft(home="LA Lakers", away="Boston Celtics", h2h_home=2.00, total_line=220.5, hours=24):
    c = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=hours)
    return dict(event_id="s1", league="nba", home=home, away=away, commence=c, markets={
        "h2h": [dict(book="sportybet", selection="home", line=None, odds=h2h_home),
                dict(book="sportybet", selection="away", line=None, odds=1.80)],
        "totals": [dict(book="sportybet", selection="over", line=total_line, odds=2.30),
                   dict(book="sportybet", selection="under", line=total_line, odds=1.60)]})


def _ref(tmp_path, raw, **kw):
    api = FakeAPI(raw)
    return PinRef(api=api, path=str(tmp_path / "pin.json"), **kw), api


def test_refresh_stores_upcoming_only_and_tracks_credits(tmp_path):
    ref, _ = _ref(tmp_path, [_raw("Los Angeles Lakers", "Boston Celtics"), _raw("A", "B", hours=-2)])
    assert ref.refresh("nba") == 1
    assert ref.spent_today() == COST_PER_REFRESH
    again = PinRef(api=None, path=str(tmp_path / "pin.json"))
    assert again.spent_today() == COST_PER_REFRESH
    assert again.rows_for(_soft())


def test_name_variants_match_and_other_game_does_not(tmp_path):
    ref, _ = _ref(tmp_path, [_raw("Los Angeles Lakers", "Boston Celtics")])
    ref.refresh("nba")
    assert ref.rows_for(_soft("LA Lakers", "Boston Celtics"))
    assert not ref.rows_for(_soft("Miami Heat", "Boston Celtics"))
    assert not ref.rows_for(_soft(hours=72))  # same teams, different date


def test_candidate_vs_real_pinnacle_price(tmp_path):
    ref, _ = _ref(tmp_path, [_raw("Los Angeles Lakers", "Boston Celtics")])
    ref.refresh("nba")
    cands = ref.candidates(_soft())
    homes = [c for c in cands if c["market"] == "h2h" and c["selection"] == "home"]
    assert len(homes) == 1
    c = homes[0]
    assert 0.50 < c["p"] < 0.56 and c["ev"] >= 0.04
    assert c["min_odds"] > c["fair_odds"] > 1 and c["book"] == "sportybet"
    assert all(x["ev"] <= 0.20 for x in cands)


def test_line_mismatch_gives_no_totals_candidate(tmp_path):
    ref, _ = _ref(tmp_path, [_raw("Los Angeles Lakers", "Boston Celtics")])
    ref.refresh("nba")
    assert not [c for c in ref.candidates(_soft(total_line=224.5)) if c["market"] == "totals"]


def test_stale_reference_is_not_used(tmp_path):
    ref, _ = _ref(tmp_path, [_raw("Los Angeles Lakers", "Boston Celtics")], stale_max_min=0)
    ref.refresh("nba")
    assert ref.candidates(_soft()) == []


def test_daily_cap_blocks_second_refresh(tmp_path):
    ref, api = _ref(tmp_path, [_raw("Los Angeles Lakers", "Boston Celtics")],
                    max_age_min=0, daily_cap=COST_PER_REFRESH)
    ref.ensure_fresh("nba")
    ref.ensure_fresh("nba")
    assert api.calls == 1


def test_fresh_cache_costs_nothing(tmp_path):
    ref, api = _ref(tmp_path, [_raw("Los Angeles Lakers", "Boston Celtics")], max_age_min=60)
    ref.ensure_fresh("nba")
    ref.ensure_fresh("nba")
    assert api.calls == 1


def test_beast_has_pinref(monkeypatch, tmp_path):
    monkeypatch.setenv("PIN_REF_PATH", str(tmp_path / "p.json"))
    from core.beast import Beast
    assert hasattr(Beast(), "pinref")


def test_soft_commence_as_string_or_epoch(tmp_path):
    ref, _ = _ref(tmp_path, [_raw("Los Angeles Lakers", "Boston Celtics")])
    ref.refresh("nba")
    c = datetime.now(timezone.utc) + timedelta(hours=24)
    for val in (c.strftime("%Y-%m-%dT%H:%M:%SZ"), int(c.timestamp()), int(c.timestamp() * 1000), "garbage", None):
        ev = _soft(); ev["commence"] = val
        assert ref.rows_for(ev)


# ---- smarter OddsPapi refreshes ----
def _papi_blob(hours_list, age_min=0):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {"fetched_at": (now - timedelta(minutes=age_min)).isoformat(), "source": "oddspapi",
            "events": [{"event_id": str(i), "commence": (now + timedelta(hours=h)).isoformat()}
                       for i, h in enumerate(hours_list)]}


def test_papi_ttl_far_game_waits_longer(tmp_path):
    ref, _ = _ref(tmp_path, [])
    assert ref._ttl_min(_papi_blob([60])) == 480        # game in 2.5 days: no call needed for 8h
    assert ref._ttl_min(_papi_blob([10])) == 240        # inside 24h: normal cadence
    assert ref._ttl_min(_papi_blob([1.5])) == 45        # inside 3h: near-tip cadence
    assert ref._ttl_min(_papi_blob([])) == 480          # nothing at all


def test_papi_ttl_in_play_game_needs_no_call(tmp_path):
    ref, _ = _ref(tmp_path, [])
    assert ref._ttl_min(_papi_blob([-1])) == 480        # started 1h ago: no refresh for it


def test_papi_game_entering_window_triggers_refresh_need(tmp_path):
    ref, _ = _ref(tmp_path, [])
    b = _papi_blob([20], age_min=300)                   # 5h old, game in 20h
    assert ref._age_min(b) >= ref._ttl_min(b)           # stale for an active league -> refresh happens
    b = _papi_blob([60], age_min=300)
    assert ref._age_min(b) < ref._ttl_min(b)            # same age, far game -> no call


def test_oddsapi_leagues_keep_old_ttl(tmp_path):
    ref, _ = _ref(tmp_path, [])
    b = _papi_blob([60]); b["source"] = "oddsapi"
    assert ref._ttl_min(b) == 240                       # unchanged for Odds API leagues


def _ev_for_mismatch(st, ss):
    return dict(league="nba", event_id="e", home="A", away="B", commence=None, markets={
        "totals": [dict(book="sporty", selection="over", line=st, odds=1.9), dict(book="sporty", selection="under", line=st, odds=1.9)],
        "spreads": [dict(book="sporty", selection="home", line=ss, odds=1.9), dict(book="sporty", selection="away", line=ss, odds=1.9)]})


def _pin_for_mismatch(pt, ps):
    return dict(event_id="p", league="nba", home="A", away="B", commence=None, markets={
        "totals": [dict(book="pinnacle", selection="over", line=pt, odds=1.9), dict(book="pinnacle", selection="under", line=pt, odds=1.9)],
        "spreads": [dict(book="pinnacle", selection="home", line=ps, odds=1.9), dict(book="pinnacle", selection="away", line=ps, odds=1.9)]})


def test_data_check_blocks_games_whose_books_cannot_be_the_same_market():
    from core.pinref import PinRef
    pr = PinRef.__new__(PinRef)
    cur = {}
    pr.match = lambda ev, allow_started=False: cur["pin"]
    ev = _ev_for_mismatch(207.5, -4.5)
    cur["pin"] = _pin_for_mismatch(206.5, -4.0)
    assert pr.mismatch(ev) is None                                   # normal 1-point gaps pass
    cur["pin"] = _pin_for_mismatch(102.5, -4.0)
    assert "totals far apart" in pr.mismatch(ev)                     # a half-game total filed as the full game
    cur["pin"] = _pin_for_mismatch(207.5, 6.5)
    assert "favourite" in pr.mismatch(ev)                            # home / away swapped
    cur["pin"] = _pin_for_mismatch(207.5, -11.0)
    assert "spreads far apart" in pr.mismatch(ev)
    assert pr.candidates(ev) == []
