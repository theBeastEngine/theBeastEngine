"""OddsPapi source: market classification, normalisation to the Odds API event shape, key rotation, PinRef wiring."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

from services import oddspapi as op
from services.client import HttpError

CAT = {
    101: dict(marketId=101, marketName="Moneyline", marketType="moneyline", period="fulltime", handicap=0,
              outcomes=[dict(outcomeId=101, outcomeName="1"), dict(outcomeId=102, outcomeName="2")]),
    1001: dict(marketId=1001, marketName="Spread -3.5", marketType="spreads", period="fulltime", handicap=-3.5,
               outcomes=[dict(outcomeId=1001, outcomeName="1"), dict(outcomeId=1002, outcomeName="2")]),
    1011: dict(marketId=1011, marketName="Spread 3.5", marketType="spreads", period="fulltime", handicap=3.5,
               outcomes=[dict(outcomeId=1011, outcomeName="1"), dict(outcomeId=1012, outcomeName="2")]),
    1100: dict(marketId=1100, marketName="Total 160.5", marketType="totals", period="fulltime", handicap=160.5,
               outcomes=[dict(outcomeId=1100, outcomeName="Over"), dict(outcomeId=1101, outcomeName="Under")]),
    2001: dict(marketId=2001, marketName="Spread -1.5 Q1", marketType="spreads", period="p1", handicap=-1.5,
               outcomes=[dict(outcomeId=2001, outcomeName="1"), dict(outcomeId=2002, outcomeName="2")]),
    3001: dict(marketId=3001, marketName="Team Total", marketType="team totals", period="fulltime", handicap=80.5,
               outcomes=[dict(outcomeId=3001, outcomeName="Over"), dict(outcomeId=3002, outcomeName="Under")]),
}


def mk(prices):
    return dict(outcomes={str(o): dict(players={"0": dict(active=True, price=p)}) for o, p in prices.items()})


def fixture(ml=(1.5, 2.6), start="2099-01-01T19:00:00.000Z", spread_hcap_main="1001"):
    return dict(fixtureId="f1", participant1Id=1, participant2Id=2, startTime=start, bookmakerOdds={"pinnacle": dict(
        bookmakerIsActive=True, markets={
            "101": mk({101: ml[0], 102: ml[1]}),
            "1001": mk({1001: 1.95, 1002: 1.95}),
            "1011": mk({1011: 1.2, 1012: 4.5}),
            "1100": mk({1100: 1.91, 1101: 1.95}),
            "2001": mk({2001: 1.9, 2002: 1.9}),
            "3001": mk({3001: 1.9, 3002: 1.9}),
            "9999": mk({1: 2.0, 2: 2.0})})})


NAMES = {"1": "Real Madrid", "2": "Barcelona"}


def test_classify():
    assert op.classify(CAT[101])[:2] == ("h2h", True)
    assert op.classify(CAT[1001]) == ("spreads", True, -3.5)
    assert op.classify(CAT[1100])[0] == "totals"
    assert op.classify(CAT[2001])[1] is False          # Q1 is not full time
    assert op.classify(CAT[3001]) is None               # team totals are not game totals
    assert op.classify(dict(marketType="1x2", period="fulltime", outcomes=[1, 2, 3])) is None


def test_normalize_shape_and_sides():
    st = {}
    evs = op.normalize([fixture()], NAMES, CAT, "acb", st)
    assert len(evs) == 1
    ev = evs[0]
    assert (ev["home"], ev["away"], ev["league"]) == ("Real Madrid", "Barcelona", "acb")
    assert ev["commence"].year == 2099
    ml = {o["selection"]: o["odds"] for o in ev["markets"]["h2h"]}
    assert ml == {"home": 1.5, "away": 2.6}
    tot = {o["selection"]: (o["line"], o["odds"]) for o in ev["markets"]["totals"]}
    assert tot == {"over": (160.5, 1.91), "under": (160.5, 1.95)}
    assert all(o["book"] == "pinnacle" for ms in ev["markets"].values() for o in ms)
    assert "9999" in st["unknown_market_ids"]


def test_quarter_and_team_total_are_excluded_but_counted():
    st = {}
    evs = op.normalize([fixture()], NAMES, CAT, "acb", st)
    lines = {o["line"] for o in evs[0]["markets"]["spreads"]}
    assert lines == {-3.5, 3.5} or lines == {3.5, -3.5}  # no -1.5 (Q1)
    assert ("spreads", "p1") in st["seen"]                # the probe can still report that Q1 exists


def test_spread_sign_flipped_when_feed_uses_other_convention():
    # home is the favourite (1.5 vs 2.6) but the main line says +3.5 for home: flip to the HOME line convention
    fx = fixture()
    fx["bookmakerOdds"]["pinnacle"]["markets"].pop("1011")
    cat = dict(CAT)
    cat[1001] = dict(CAT[1001], handicap=3.5)
    st = {}
    ev = op.normalize([fx], NAMES, cat, "acb", st)[0]
    assert st.get("spread_flipped") == 1
    assert {o["line"] for o in ev["markets"]["spreads"]} == {-3.5}


def test_spread_sign_kept_when_consistent():
    st = {}
    op.normalize([fixture()], NAMES, CAT, "acb", st)
    assert st.get("spread_ok") == 1 and not st.get("spread_flipped")


def test_missing_names_skipped_not_crash():
    st = {}
    assert op.normalize([fixture()], {}, CAT, "acb", st) == [] and st["no_names"] == 1


def test_inactive_and_bad_prices_ignored():
    fx = fixture()
    fx["bookmakerOdds"]["pinnacle"]["markets"]["1100"]["outcomes"]["1100"]["players"]["0"]["active"] = False
    fx["bookmakerOdds"]["pinnacle"]["markets"]["1100"]["outcomes"]["1101"]["players"]["0"]["price"] = 0.9
    ev = op.normalize([fx], NAMES, CAT, "acb")[0]
    assert "totals" not in ev["markets"]


# --- client ---
class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def client(get, keys=("k1", "k2", "k3"), spacing=6):
    c = Clock()
    return op.OddsPapi(keys=list(keys), spacing=spacing, get=get, sleep=c.sleep, clock=c.now), c


def test_round_robin_over_keys_and_spacing():
    used = []
    cl, clock = client(lambda url, p: (used.append(p["apiKey"]) or [], {}))
    for _ in range(4):
        cl.call("/v4/x")
    assert used == ["k1", "k2", "k3", "k1"]
    assert clock.slept == [6.0]  # only the 4th call (k1 reused) had to wait the 6 s spacing


def test_key_retired_on_401_and_error_is_masked():
    def get(url, p):
        if p["apiKey"] == "k1":
            raise HttpError(401, "bad key k1")
        return [], {}
    cl, _ = client(get)
    cl.call("/v4/x")
    assert "k1" in cl.exhausted and cl.calls == 1


def test_all_keys_exhausted_raises():
    cl, _ = client(lambda u, p: (_ for _ in ()).throw(HttpError(403, "no")), keys=("a",))
    try:
        cl.call("/v4/x")
        assert False
    except op.NoKeys:
        pass


def test_429_retries_then_fails_with_masked_key():
    def get(url, p):
        raise HttpError(429, "slow down " + p["apiKey"])
    cl, _ = client(get, keys=("secretkey",))
    try:
        cl.call("/v4/x")
        assert False
    except HttpError as e:
        assert "secretkey" not in str(e)


# --- PinRef wiring ---
class FakeClient:
    def __init__(self):
        self.calls = 0

    def catalog(self):
        self.calls += 1
        return CAT

    def participants(self):
        self.calls += 1
        return NAMES

    def odds(self, tids):
        self.calls += 1
        assert tids == [264]
        return [fixture()]


def test_pinref_refresh_papi_and_candidates(monkeypatch):
    from core.pinref import PinRef
    monkeypatch.setenv("ODDSPAPI_KEYS", "k")
    p = PinRef(path=os.path.join(tempfile.mkdtemp(), "pr.json"))
    p._papi = FakeClient()
    assert p.ensure_fresh("acb") is True
    assert p._papi.calls == 3 and p._papi_spent_today() == 3
    ev = dict(event_id="sb1", league="acb", home="Real Madrid", away="FC Barcelona",
              commence=datetime(2099, 1, 1, 19, 0), markets={"totals": [
                  dict(book="sportybet", selection="over", line=160.5, odds=2.30),
                  dict(book="sportybet", selection="under", line=160.5, odds=1.60)]})
    pick = p.candidates(ev)
    assert pick and pick[0]["selection"] == "over" and pick[0]["line"] == 160.5 and pick[0]["ev"] > 0.04
    assert p.pregame_params(ev) is not None
    # second call inside the TTL spends nothing
    assert p.ensure_fresh("acb") is True and p._papi_spent_today() == 3


def test_pinref_papi_daily_cap(monkeypatch):
    from core.pinref import PinRef
    monkeypatch.setenv("ODDSPAPI_KEYS", "k")
    monkeypatch.setenv("PAPI_DAILY_CAP", "2")
    p = PinRef(path=os.path.join(tempfile.mkdtemp(), "pr.json"))
    p._papi = FakeClient()
    assert p.ensure_fresh("acb") is False and p._papi.calls == 0


def test_pinref_papi_needs_keys(monkeypatch):
    from core.pinref import PinRef
    monkeypatch.delenv("ODDSPAPI_KEYS", raising=False)
    monkeypatch.delenv("ODDSPAPI_KEY", raising=False)
    p = PinRef(path=os.path.join(tempfile.mkdtemp(), "pr.json"))
    assert p.ensure_fresh("acb") is False


def test_league_sources():
    from config.leagues import pin_source
    assert pin_source("nba") == "odds_api" and pin_source("acb") == "oddspapi" and pin_source("g_league") is None


def test_real_catalog_labels_full_game_is_result():
    """Probe of the real catalog: full game incl. overtime has period 'result'; first half is 'p1+p2'."""
    outs = [dict(outcomeId=1, outcomeName="Home"), dict(outcomeId=2, outcomeName="Away")]
    mk = dict(marketType="spreads", marketName="Handicap (incl. overtime)", period="result", handicap=5.5, outcomes=outs)
    assert op.classify(mk) == ("spreads", True, 5.5)
    tot = dict(marketType="totals", marketName="Over Under (incl. overtime)", period="result", handicap=170.0,
               outcomes=[dict(outcomeId=1, outcomeName="Over"), dict(outcomeId=2, outcomeName="Under")])
    assert op.classify(tot) == ("totals", True, 170.0)
    ml = dict(marketType="moneyline", marketName="Winner (incl. overtime)", period="result", handicap=0.0, outcomes=outs)
    assert op.classify(ml) == ("h2h", True, 0.0)
    half = dict(marketType="moneyline", marketName="First Half Winner", period="p1+p2", handicap=0.0, outcomes=outs)
    assert op.classify(half) is None or op.classify(half)[1] is False
    halft = dict(marketType="totals", marketName="Over Under First Half", period="p1+p2", handicap=85.5,
                 outcomes=tot["outcomes"])
    assert op.classify(halft) is None or op.classify(halft)[1] is False


def test_papi_empty_blob_with_fixtures_is_retried(monkeypatch):
    from core.pinref import PinRef
    monkeypatch.setenv("ODDSPAPI_KEYS", "k")
    p = PinRef(path=os.path.join(tempfile.mkdtemp(), "pr.json"))
    p._papi = FakeClient()
    old = (datetime.utcnow() - timedelta(minutes=60)).isoformat()
    # old-style blob (no raw_fixtures field), empty: must be refetched
    p.data["leagues"]["acb"] = {"fetched_at": old, "events": [], "source": "oddspapi"}
    assert p.ensure_fresh("acb") is True and p._papi.calls == 3
    # a truly idle league (raw_fixtures == 0) is NOT retried inside its normal TTL
    p2 = PinRef(path=os.path.join(tempfile.mkdtemp(), "pr2.json"))
    p2._papi = FakeClient()
    p2.data["leagues"]["acb"] = {"fetched_at": old, "events": [], "source": "oddspapi", "raw_fixtures": 0}
    assert p2.ensure_fresh("acb") is True and p2._papi.calls == 0


# ---- NBA preseason: OddsPapi adds games The Odds API does not price (separate cache, own refresh) ----
class _ExtraClient(FakeClient):
    def __init__(self, hours=30):
        super().__init__()
        self.hours = hours

    def odds(self, tids):
        self.calls += 1
        assert tids == [2382, 132]   # NBA preseason + regular season
        start = (datetime.now(timezone.utc) + timedelta(hours=self.hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return [fixture(start=start)]


def _nba_pin(papi):
    from core.pinref import PinRef
    p = PinRef(path=os.path.join(tempfile.mkdtemp(), "pr.json"))
    p._papi = papi
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Odds API side is fresh and idle (games only from 20 Oct): the main refresh must not run
    p.data["leagues"]["nba"] = {"fetched_at": now.isoformat(), "events": [dict(
        event_id="oa1", league="nba", home="LA Lakers", away="Boston Celtics",
        commence=(now + timedelta(days=9)).isoformat(), markets={})]}
    return p


def test_nba_extra_games_are_added_and_matched(monkeypatch):
    monkeypatch.setenv("ODDSPAPI_KEYS", "k")
    p = _nba_pin(_ExtraClient())
    assert p.ensure_fresh("nba") is True
    assert p._papi.calls == 3 and p._papi_spent_today() == 3
    assert [e["home"] for e in p.data["extras"]["nba"]["events"]] == ["Real Madrid"]
    assert [e["home"] for e in p.data["leagues"]["nba"]["events"]] == ["LA Lakers"]   # main cache untouched
    ev = dict(event_id="sb1", league="nba", home="Real Madrid", away="FC Barcelona",
              commence=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=30), markets={})
    assert p.match(ev) is not None and p.pregame_params(ev) is not None


def test_nba_extra_is_not_refetched_inside_ttl(monkeypatch):
    monkeypatch.setenv("ODDSPAPI_KEYS", "k")
    p = _nba_pin(_ExtraClient())
    p.ensure_fresh("nba")
    calls = p._papi.calls
    assert p.ensure_fresh("nba") is True and p._papi.calls == calls


def test_nba_extra_failure_never_breaks_main(monkeypatch):
    monkeypatch.setenv("ODDSPAPI_KEYS", "k")

    class Boom(FakeClient):
        def catalog(self):
            raise HttpError(500, "down")

    p = _nba_pin(Boom())
    assert p.ensure_fresh("nba") is True          # main (Odds API) data still usable
    assert [e["home"] for e in p.data["leagues"]["nba"]["events"]] == ["LA Lakers"]


def test_nba_extra_needs_keys(monkeypatch):
    monkeypatch.delenv("ODDSPAPI_KEYS", raising=False)
    monkeypatch.delenv("ODDSPAPI_KEY", raising=False)
    p = _nba_pin(_ExtraClient())
    assert p.ensure_fresh("nba") is True and p._papi.calls == 0


def test_nba_extra_respects_daily_cap(monkeypatch):
    monkeypatch.setenv("ODDSPAPI_KEYS", "k")
    monkeypatch.setenv("PAPI_DAILY_CAP", "2")
    p = _nba_pin(_ExtraClient())
    p.ensure_fresh("nba")
    assert p._papi.calls == 0
