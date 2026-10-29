import json

from tennis import cards, model, names, pin as tpin, results, sporty
from tennis.engine import Engine

NOW = 1_800_000_000.0


# ------------------------------------------------------------------ names
def test_player_names_across_feeds():
    s = names.same_side
    assert s("Djokovic, Novak", "Novak Djokovic") and s("Djokovic, Novak", "Djokovic N.") and s("Novak Djokovic", "N. Djokovic")
    assert s("De Minaur, Alex", "Alex de Minaur")
    assert not s("Williams, Venus", "Serena Williams")                    # same surname, clashing first names
    assert not s("Sinner, Jannik", "Alcaraz, Carlos")
    sc, sw = names.pair_score("Djokovic, Novak", "Alcaraz, Carlos", "Carlos Alcaraz", "Novak Djokovic")
    assert sc > 0.9 and sw is True                                        # same match, listed the other way round
    assert names.pair_score("Sinner, Jannik", "Zverev, Alexander", "Carlos Alcaraz", "Novak Djokovic")[0] == 0.0
    assert names.same_side("Bolelli, Simone / Vavassori, Andrea", "Andrea Vavassori / Simone Bolelli")
    assert names.bad_league("ITF Juniors Boys") and names.bad_league("Table Tennis") and not names.bad_league("ATP Paris Masters")


# ------------------------------------------------------------------ model
SOFT = {"ml": {"home": 1.95, "away": 1.85}, "total": {22.5: {"over": 1.85, "under": 2.0}, 23.5: {"over": 2.2, "under": 1.65}},
        "ah": {-3.5: {"home": 1.90, "away": 1.90}}}
PIN = {"ml": {"home": 1.75, "away": 2.20}, "total": {22.5: {"over": 2.10, "under": 1.75}}, "ah": {-3.5: {"home": 1.80, "away": 2.00}}}


def test_settle_match_winner_total_and_handicap_games():
    s = model.settle
    assert s(dict(mkt="ml", line=None, sel="home", odds=2.0), 14, 20, True) == ("won", 1.0)
    assert s(dict(mkt="ml", line=None, sel="away", odds=2.0), 14, 20, True) == ("lost", -1.0)
    assert s(dict(mkt="total", line=22.5, sel="over", odds=1.9), 14, 10)[0] == "won"
    assert s(dict(mkt="total", line=22.0, sel="under", odds=1.9), 12, 10)[0] == "void"
    assert s(dict(mkt="ah", line=-3.5, sel="home", odds=1.9), 14, 10)[0] == "won"
    assert s(dict(mkt="ah", line=-3.5, sel="away", odds=1.9), 14, 10)[0] == "lost"
    assert s(dict(mkt="ah", line=2.5, sel="away", odds=1.9), 10, 14)[0] == "won"       # away -2.5 side: shown as +2.5 line for home


def test_candidates_compare_identical_lines_only_and_gates():
    cs = model.candidates(SOFT, PIN)
    keys = {model.pkey(c) for c in cs}
    assert "total|23.5|over" not in keys and "total|22.5|under" in keys and "ml||home" in keys
    assert next(c for c in cs if model.pkey(c) == "ml||home")["why_not"] is None
    assert next(c for c in cs if model.pkey(c) == "total|22.5|under")["ev"] > 0.04
    assert next(c for c in cs if model.pkey(c) == "ah|-3.5|home")["why_not"]             # no edge


def test_flip_turns_everything_around():
    f = model.flip(dict(home="A", away="B", markets=dict(ml={"home": 1.5, "away": 2.5}, ah={-3.5: {"home": 1.9, "away": 1.9}},
                                                       total={22.5: {"over": 1.9, "under": 1.9}})))
    assert f["home"] == "B" and f["markets"]["ml"] == {"away": 1.5, "home": 2.5} and 3.5 in f["markets"]["ah"]
    assert model.describe(dict(mkt="ah", line=-3.5, sel="home"), "A", "B") == "A -3.5 games"
    assert model.describe(dict(mkt="ah", line=-3.5, sel="away"), "A", "B") == "B +3.5 games"


def test_agreement_blocks_other_match():
    assert model.agree(SOFT, PIN) is None
    assert model.agree({"ml": {"home": 1.2, "away": 4.5}}, {"ml": {"home": 3.5, "away": 1.3}})


# ------------------------------------------------------------------ SportyBet parse
def test_sporty_parse_by_name_and_player():
    pl = {"data": {"tournaments": [
        {"id": "t1", "categoryName": "ATP", "name": "Paris, France", "events": [
            {"eventId": "sr:match:9", "homeTeamName": "Djokovic, Novak", "awayTeamName": "Alcaraz, Carlos", "estimateStartTime": int(NOW * 1000), "markets": [
                {"id": 186, "desc": "Winner", "outcomes": [{"id": "4", "desc": "Djokovic, Novak", "odds": "1.95"}, {"id": "5", "desc": "Alcaraz, Carlos", "odds": "1.85"}]},
                {"id": 188, "desc": "Total games", "specifier": "total=22.5", "outcomes": [{"id": "12", "desc": "Over 22.5", "odds": "1.85"}, {"id": "13", "desc": "Under 22.5", "odds": "2.05"}]},
                {"id": 187, "desc": "Handicap games", "specifier": "hcp=-3.5", "outcomes": [{"id": "1714", "desc": "Djokovic, Novak (-3.5)", "odds": "1.90"}, {"id": "1715", "desc": "Alcaraz, Carlos (+3.5)", "odds": "1.90"}]},
                {"id": 202, "desc": "1st set winner", "outcomes": [{"id": "4", "desc": "Djokovic, Novak", "odds": "1.5"}, {"id": "5", "desc": "Alcaraz, Carlos", "odds": "2.5"}]},
                {"id": 190, "desc": "Set handicap", "specifier": "hcp=-1.5", "outcomes": [{"id": "1714", "desc": "A", "odds": "3.0"}, {"id": "1715", "desc": "B", "odds": "1.3"}]}]}]},
        {"id": "t2", "categoryName": "ITF", "name": "Juniors Boys", "events": [{"eventId": "x", "homeTeamName": "A", "awayTeamName": "B", "estimateStartTime": int(NOW * 1000)}]}]}}
    evs = sporty.parse_events(pl)
    assert len(evs) == 1
    m = evs[0]["markets"]
    assert m["ml"] == {"home": 1.95, "away": 1.85} and m["total"][22.5] == {"over": 1.85, "under": 2.05} and m["ah"][-3.5] == {"home": 1.9, "away": 1.9}
    assert len(m["ah"]) == 1                                                         # the set handicap is ignored


# ------------------------------------------------------------------ Pinnacle normalize
def test_normalize_odds_api_tennis_event():
    raw = {"id": "e1", "home_team": "Novak Djokovic", "away_team": "Carlos Alcaraz", "commence_time": "2027-01-15T15:00:00Z", "bookmakers": [
        {"key": "pinnacle", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Novak Djokovic", "price": 1.75}, {"name": "Carlos Alcaraz", "price": 2.2}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": 2.1, "point": 22.5}, {"name": "Under", "price": 1.75, "point": 22.5}]},
            {"key": "spreads", "outcomes": [{"name": "Novak Djokovic", "price": 1.8, "point": -3.5}, {"name": "Carlos Alcaraz", "price": 2.0, "point": 3.5}]}]}]}
    ev = tpin.normalize_oa(raw)
    assert ev["markets"]["ml"] == {"home": 1.75, "away": 2.2} and ev["markets"]["total"][22.5] == {"over": 2.1, "under": 1.75}
    assert ev["markets"]["ah"][-3.5] == {"home": 1.8, "away": 2.0}


def test_classify_papi_tennis():
    c = tpin.classify_tn
    assert c(dict(marketType="moneyline", marketName="Match Winner", period="result", outcomes=[1, 2])) == ("ml", None)
    assert c(dict(marketType="totals", marketName="Total Games", period="result", handicap=22.5, outcomes=[1, 2])) == ("total", 22.5)
    assert c(dict(marketType="spreads", marketName="Handicap Games", period="result", handicap=-3.5, outcomes=[1, 2])) == ("ah", -3.5)
    assert c(dict(marketType="spreads", marketName="Set Handicap", period="result", handicap=-1.5, outcomes=[1, 2])) == ("sh", -1.5)
    assert c(dict(marketType="spreads", marketName="Set Handicap", period="p1", handicap=-1.5, outcomes=[1, 2])) is None
    assert c(dict(marketType="moneyline", marketName="1st Set Winner", period="result", outcomes=[1, 2])) is None
    assert c(dict(marketType="1x2", marketName="Full Time Result", period="result", handicap=0, outcomes=[1, 2])) == ("ml", None)
    assert c(dict(marketType="spreads", marketName="Asian Handicap", period="result", handicap=0, outcomes=[1, 2])) is None
    assert c(dict(marketType="totals", marketName="Total Aces", period="result", handicap=10.5, outcomes=[1, 2])) is None


# ------------------------------------------------------------------ Sofascore results
def test_results_parse_retirement_is_void_and_games_are_summed():
    r = results.parse({"events": [
        {"homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}, "homeScore": {"current": 2, "period1": 6, "period2": 7}, "awayScore": {"current": 0, "period1": 4, "period2": 6},
         "winnerCode": 1, "status": {"type": "finished", "description": "Ended"}, "startTimestamp": 5},
        {"homeTeam": {"name": "C"}, "awayTeam": {"name": "D"}, "homeScore": {"current": 1, "period1": 6}, "awayScore": {"current": 0, "period1": 2},
         "winnerCode": 1, "status": {"type": "finished", "description": "Retired"}, "startTimestamp": 6}]})
    assert r[0]["state"] == "finished" and r[0]["hg"] == 13 and r[0]["ag"] == 10 and r[0]["home_won"] is True
    assert r[1]["state"] == "void"


# ------------------------------------------------------------------ engine
class FakeFeed:
    def __init__(self, ev):
        self.events = {ev["id"]: ev}
        self.last = {}

    def refresh(self, today_only=False, force=False):
        return 0

    def upcoming(self, h, now=None):
        now = now or NOW
        return [e for e in self.events.values() if now - 600 <= e["t"] <= now + h]


class FakePin:
    def __init__(self, ev):
        self.link = {ev["id"]: ("oa:tennis_atp_paris", "p1", False)}
        self.papi = None
        self.markets = PIN
        self.age = 3.0
        self.spend = {"oa": 0, "pp": 0}
        self.calls = []

    def refresh_index(self, events, horizon):
        return 0

    def papi_link(self, events):
        return 0

    def get(self, ev, ttl, force=None):
        self.calls.append(force)
        return dict(id="p1", home=ev["home"], away=ev["away"], t=ev["t"], markets=self.markets), self.age, ""


class FakeResults:
    r = None

    def find(self, home, away, t):
        return self.r


class Cards:
    def __init__(self):
        self.d = {}

    def put(self, key, text, day, silent=True, data=None, reply_to=None):
        self.d[key] = text
        self.replies = getattr(self, "replies", {})
        self.replies[key] = reply_to
        self.ids = getattr(self, "ids", {})
        self.ids.setdefault(key, 500 + len(self.ids))

    def get(self, key):
        return {"msg_id": getattr(self, "ids", {}).get(key)} if key in getattr(self, "ids", {}) else {}

    def _st(self):
        return {}


def rig(tip_in=4 * 3600):
    ev = dict(id="sr:match:9", lid="T1", lname="ATP - Paris, France", country="ATP", tname="Paris, France", home="Djokovic, Novak",
              away="Alcaraz, Carlos", t=NOW + tip_in,
              markets={"ml": dict(SOFT["ml"]), "total": {k: dict(v) for k, v in SOFT["total"].items()}, "ah": {k: dict(v) for k, v in SOFT["ah"].items()}})
    clock = [NOW]
    sent, edits, plain, store = [], [], [], {}
    from core.cplog import CPLog
    feed, pn, res = FakeFeed(ev), FakePin(ev), FakeResults()

    def send(text, markup, reply_to, silent):
        sent.append(dict(text=text, markup=markup, reply_to=reply_to, silent=silent))
        return 100 + len(sent)

    def edit(mid, text, markup):
        edits.append(dict(mid=mid, text=text, markup=markup))
        return True
    eng = Engine(lambda k: store.get(k), lambda k, v: store.__setitem__(k, v), feed, pn, res, send, edit,
                 lambda t, s=False: plain.append(t), Cards(), cplog=CPLog(), now=lambda: clock[0])
    return eng, feed, pn, res, sent, edits, plain, clock, ev


def at(eng, clock, t):
    clock[0] = t
    eng.tick()


def test_tennis_flow_play_now_verdicts_summary_and_grading():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    assert len(sent) == 1 and sent[0]["silent"] is True and "Paris" in sent[0]["text"] and "waiting for a second check" in sent[0]["text"]
    assert "MATCH WINNER" in sent[0]["text"] and "GAME HANDICAP" in sent[0]["text"] and "TOTAL GAMES" in sent[0]["text"]
    assert "SportyBet | Pinnacle fair | edge" in sent[0]["text"] and "POSSIBLE BET: Under 22.5 games" in sent[0]["text"]
    assert "Djokovic -3.5 " in sent[0]["text"] and "Under 22.5 " in sent[0]["text"]
    assert not eng.picks["picks"]
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    assert any("PLAY NOW" in s["text"] for s in sent) and len(eng.picks["picks"]) == 2          # match winner + total games under
    for kind in ("t60", "final", "t15", "last"):
        at(eng, clock, next(s["t"] for s in m["slots"] if s["kind"] == kind) + 1)
        assert "%s VERDICT" % cards.LATE_NAME[kind] in sent[-1]["text"] and sent[-1]["silent"] is False
    assert "BOTTOM LINE: play 2 of 2" in sent[-1]["text"]
    assert "SUMMARY" in edits[-1]["text"] and "Markets to take" in edits[-1]["text"]
    clock[0] = ev["t"] + 5 * 3600
    assert eng.grade() == 0
    res.r = dict(state="finished", hg=13, ag=10, home_won=True)                  # home wins 2-0: 23 games -> total under 22.5 loses
    assert eng.grade() == 2
    st = {p["desc"]: p["status"] for p in eng.picks["picks"].values()}
    assert st["Djokovic, Novak to win"] == "won" and st["Under 22.5 games"] == "lost"
    assert eng.stats()["graded"] == 2


def test_retirement_is_void_and_stays_out_of_the_win_rate():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    clock[0] = ev["t"] + 5 * 3600
    res.r = dict(state="void", hg=6, ag=2, home_won=True)
    assert eng.grade() == 2 and eng.stats()["graded"] == 0
    assert all(p["status"] == "void" for p in eng.picks["picks"].values())


def test_unpriced_match_gets_no_card():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    pn.link = {}
    at(eng, clock, NOW)
    assert not sent and not eng.matches


def test_bot_commands():
    from tennis.bot import TennisBot
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    bot = TennisBot(eng)
    for cmd in ("/status", "/open", "/bets", "/winrate", "/bankroll", "/checkpoints", "/today", "/agenda", "/leagues", "/keys", "/markets", "/unlinked", "/help", "hello"):
        out = bot.handle_text(cmd)
        assert out and "Error" not in str(out), (cmd, out)
    assert bot.handle_callback("ptog:1:1,2")[0]


def test_sporty_feed_falls_back_to_another_request_shape_on_422():
    from services.client import HttpError
    calls = []
    page = {"data": {"tournaments": [{"id": "t1", "categoryName": "ATP", "name": "Paris", "events": [
        {"eventId": "e1", "homeTeamName": "A, B", "awayTeamName": "C, D", "estimateStartTime": int(NOW * 1000), "markets": []}]}]}}

    def get(url, params, headers, timeout, retries):
        calls.append(dict(params))
        if params.get("marketId") == "" or params.get("pageSize") == 50 or "marketId" not in params and params.get("pageSize") != 30:
            raise HttpError(422, "Invalid")
        if params["pageNum"] > 1:
            return {"data": {"tournaments": []}}, {}
        return page, {}
    f = sporty.SportyFeed(get=get, clock=lambda: NOW)
    f.MAX_PAGES = 3
    assert f.refresh(force=True) == 1 and f.variant == 2 and "e1" in f.events
    calls.clear()
    f.refresh(force=True)
    assert all("marketId" not in c for c in calls)                      # the working shape is remembered


def test_total_games_recognised_under_other_names():
    mk = lambda desc, sp, a, b: {"id": 1, "desc": desc, "specifier": sp, "outcomes": [{"id": "12", "desc": a, "odds": "1.9"}, {"id": "13", "desc": b, "odds": "1.9"}]}
    m = sporty.parse_markets([mk("Over/Under", "total=21.5", "Over 21.5", "Under 21.5"), mk("Total", "total=2.5", "Over 2.5", "Under 2.5"),
                              mk("Games Over/Under", "total=22.5", "Over 22.5", "Under 22.5")], "A, B", "C, D")
    assert set(m["total"]) == {21.5, 22.5}                                   # a 2.5 total is sets, never games


def test_grading_from_odds_api_scores_by_event_id_and_orientation():
    from tennis.results import Results

    class OA:
        def scores(self, key, days):
            return [{"id": "ev1", "completed": True, "home_team": "Novak Djokovic", "away_team": "Carlos Alcaraz",
                     "scores": [{"name": "Novak Djokovic", "score": "2"}, {"name": "Carlos Alcaraz", "score": "1"}]},
                    {"id": "ev2", "completed": False, "home_team": "A", "away_team": "B", "scores": None}]
    r = Results(oa=OA())
    assert r.find_oa(dict(oa_key="tennis_atp_x", oa_id="ev1", oa_sw=False))["home_won"] is True
    assert r.find_oa(dict(oa_key="tennis_atp_x", oa_id="ev1", oa_sw=True))["home_won"] is False      # SportyBet lists the players the other way round
    assert r.find_oa(dict(oa_key="tennis_atp_x", oa_id="ev2")) is None and r.find_oa(dict(oa_key="k", oa_id="zz")) is None
    assert r.find_oa(dict(oa_id="ev1")) is None


def test_engine_grades_match_winner_from_scores_and_leaves_games_markets_open():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    for p in eng.picks["picks"].values():
        p.update(oa_key="tennis_atp_x", oa_id="ev1", oa_sw=False)
    res.find_oa = lambda p: dict(state="finished", hg=None, ag=None, home_won=True)
    clock[0] = ev["t"] + 5 * 3600
    assert eng.grade() == 1                                           # only the match-winner pick; "Under 22.5 games" waits for /result
    st = {p["mkt"]: p["status"] for p in eng.picks["picks"].values()}
    assert st["ml"] == "won" and st["total"] in ("new", "placed")


def test_a_players_own_total_games_is_never_the_match_total():
    mk = lambda desc, sp, a, b: {"id": 1, "desc": desc, "specifier": sp, "outcomes": [{"id": "12", "desc": a, "odds": "2.45"}, {"id": "13", "desc": b, "odds": "1.5"}]}
    m = sporty.parse_markets([mk("Cobolli, Flavio total games", "total=12.5", "Over 12.5", "Under 12.5"),
                              mk("Total games", "total=21.5", "Over 21.5", "Under 21.5"),
                              mk("Nakashima handicap games", "hcp=-2.5", "A (-2.5)", "B (+2.5)")], "Cobolli, Flavio", "Nakashima, Brandon")
    assert set(m.get("total", {})) == {21.5} and "ah" not in m


def test_an_impossibly_low_total_is_dropped_and_old_bad_picks_are_voided():
    assert 12.5 not in model.complete(dict(markets=dict(total={12.5: {"over": 1.5, "under": 2.45}, 21.5: {"over": 1.9, "under": 1.9}})))["total"]
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    eng.kv_set("tn_picks", json.dumps({"next": 2, "picks": {"1": dict(id=1, mkt="total", line=12.5, sel="under", status="new", ret=None, score=None)}}))
    eng.picks = None
    eng._load()
    assert eng.picks["picks"]["1"]["status"] == "void" and eng.picks["picks"]["1"]["score"] == "invalid market"


def test_papi_discovery_leaves_a_reserve_and_ignores_far_games():
    from tennis import pin as tp
    now = 1_000_000.0
    pulls = []

    class Papi:
        calls = 0
        def call(self, path, params=None):
            return [{"tournamentId": 7, "tournamentName": "Lyon", "categoryName": "ATP"}]

    pb = tp.PinBook(papi_client=Papi(), load=lambda k, d=None: d, save=lambda k, v: None, clock=lambda: now)
    pb.papi_tours = {"t": now, "rows": [{"tournamentId": 7}]}
    pb._tours_for = lambda se: [7]
    pb._pull_pp = lambda src, reserve=0: pulls.append((src, reserve)) or (_ for _ in ()).throw(tp.oa.OutOfCredits("OddsPapi daily budget for tennis reached"))
    far = dict(id="far", home="A", away="B", t=now + 20 * 3600)
    near = dict(id="near", home="C", away="D", t=now + 2 * 3600)
    pb.papi_link([far, near])
    assert pulls == [("pp:7", 40)]                       # the far game is not asked about; the pull keeps 40 calls in reserve


def test_oddspapi_monthly_allowance_from_account_and_fallback():
    from services.oddspapi import OddsPapi
    assert OddsPapi.parse_account({"request_limit": 250, "requests_used": 40}) == (250, 40)
    assert OddsPapi.parse_account({"plan": {"requestLimit": 250, "requestsRemaining": 200}}) == (250, 50)
    assert OddsPapi.parse_account({"hello": 1}) is None
    acct = {"api_key": "x", "current_subscription_id": "s2", "subscriptions": [
        {"subscription_id": "s1", "request_limit": 100, "request_count": 100, "is_active": False},
        {"subscription_id": "s2", "request_limit": 250, "request_count": 31, "is_active": True}]}
    assert OddsPapi.parse_account(acct) == (250.0, 31.0)
    t = [1_792_800_000.0]                                               # 24 Oct 2026: 8 days left in the month
    api = OddsPapi(keys=["a", "b"], get=lambda u, p: ({"request_limit": 250, "requests_used": 90}, {}), clock=lambda: t[0], sleep=lambda s: None)
    assert abs(api.daily_allowance() - (2 * 160) / 8) < 0.01 and abs(api.daily_allowance(24) - (2 * 160 + 24) / 8) < 0.01
    api2 = OddsPapi(keys=["a", "b", "c", "d", "e"], get=lambda u, p: (_ for _ in ()).throw(RuntimeError("x")), clock=lambda: t[0], sleep=lambda s: None)
    assert abs(api2.daily_allowance() - 5 * 250 / 31) < 0.01             # unknown quota: 250 per key per month


def test_quota_probe_async_does_not_block_and_fills_in():
    import time as _t
    from services.oddspapi import OddsPapi
    api = OddsPapi(keys=["a"], get=lambda u, p: ({"request_limit": 250, "request_count": 10}, {}), sleep=lambda s: None)
    api.probe_quota_async()
    for _ in range(50):
        if api.quota_rows:
            break
        _t.sleep(0.02)
    assert api.quota_rows == [(250.0, 10.0)]


def test_quota_client_is_shared_and_reads_keys_from_env(monkeypatch=None):
    import os
    from services import oddspapi as op
    op._QC.clear()
    os.environ["ODDSPAPI_KEYS"] = "k1,k2"
    c = op.quota_client()
    assert c is op.quota_client() and c.keys == ["k1", "k2"]
    op._QC.clear()


def test_plain_result_says_how_far_the_closest_market_is_and_extras_are_listed():
    cs = model.candidates(dict(markets=dict(ml={"home": 1.95, "away": 1.85}, total={22.5: {"over": 1.85, "under": 1.95}})),
                          dict(markets=dict(ml={"home": 1.90, "away": 1.95}, total={22.5: {"over": 1.90, "under": 1.90}})))
    txt = cards.plain_result(cs, [c for c in cs if c["why_not"] is None], "A, B", "C, D")
    assert txt.startswith("NO BET. Closest to a bet:") and "points short of the 4% needed" in txt
    ms = [{"desc": "Set handicap", "specifier": "hcp=-1.5", "outcomes": []}, {"desc": "Total sets", "specifier": "total=2.5", "outcomes": []},
          {"desc": "First set winner", "outcomes": []}, {"desc": "Winner", "outcomes": []}]
    assert sporty.extras(ms) == ["Set handicap", "Total sets", "First set winner"]


def test_extra_oddspapi_keys_are_added_without_duplicates():
    import os
    from services import oddspapi as op
    os.environ["ODDSPAPI_KEYS"] = "a,b"
    os.environ["ODDSPAPI_KEYS_EXTRA"] = "c, b ,d"
    try:
        assert op.keys_from_env() == ["a", "b", "c", "d"]
    finally:
        os.environ.pop("ODDSPAPI_KEYS_EXTRA", None)


def test_set_handicap_is_parsed_flipped_settled_and_graded_from_sets():
    mk = lambda desc, sp, a, b: {"id": 1, "desc": desc, "specifier": sp, "outcomes": [{"id": "4", "desc": a, "odds": b[0]}, {"id": "5", "desc": "x", "odds": b[1]}]}
    m = sporty.parse_markets([mk("Set Handicap", "hcp=-1.5", "Home", ("2.10", "1.70"))], "A, B", "C, D")
    assert m["sh"][-1.5] == {"home": 2.1, "away": 1.7}
    f = model.flip(dict(home="A", away="B", markets=m))
    assert f["markets"]["sh"][1.5] == {"home": 1.7, "away": 2.1}
    c = dict(mkt="sh", line=-1.5, sel="home", odds=2.1)
    assert model.settle(c, 12, 5, True, (2, 0))[0] == "won" and model.settle(c, 12, 9, True, (2, 1))[0] == "lost"
    assert model.settle(dict(c, sel="away"), 12, 9, True, (2, 1))[0] == "won" and model.settle(c, 0, 0, True) is None
    assert model.describe(dict(mkt="sh", line=-1.5, sel="home"), "Moutet", "Machac") == "Moutet -1.5 sets"
    cs = model.candidates(dict(markets=dict(sh={-1.5: {"home": 2.1, "away": 1.7}})), dict(markets=dict(sh={-1.5: {"home": 2.0, "away": 1.8}})))
    assert {c["mkt"] for c in cs} == {"sh"} and len(cs) == 2


def test_engine_grades_a_set_handicap_from_the_sets_score():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    p0 = next(iter(eng.picks["picks"].values()))
    p0.update(mkt="sh", line=-1.5, sel="home", odds=2.0, desc="Home -1.5 sets", status="new")
    res.find_oa = lambda p: dict(state="finished", hg=None, ag=None, home_won=True, hs=2, as_=0)
    clock[0] = ev["t"] + 5 * 3600
    eng.grade()
    assert p0["status"] == "won" and p0["score"] == "2-0 sets"


def test_exact_sets_and_correct_score_are_matched_by_outcome_names_and_graded_from_sets():
    cs = {"id": 1, "desc": "Correct Score", "specifier": "variant=sr:correct_score",
          "outcomes": [{"id": "a", "desc": "2:0", "odds": "2.25"}, {"id": "b", "desc": "2:1", "odds": "3.55"},
                       {"id": "c", "desc": "1:2", "odds": "5.50"}, {"id": "d", "desc": "0:2", "odds": "4.70"}]}
    es = {"id": 2, "desc": "Exact Sets", "specifier": "variant=sr:exact_sets:be",
          "outcomes": [{"id": "a", "desc": "2", "odds": "1.60"}, {"id": "b", "desc": "3", "odds": "2.25"}]}
    games_cs = dict(cs, outcomes=[{"id": "a", "desc": "6:0", "odds": "9.0"}, {"id": "b", "desc": "6:1", "odds": "8.0"}])   # a set-level 'correct score': ignored
    m = sporty.parse_markets([cs, es, games_cs], "A, B", "C, D")
    assert m["cs"]["2:0"] == 2.25 and m["es"] == {"s2": 1.6, "s3": 2.25} and "6:0" not in m["cs"]
    f = model.flip(dict(home="A", away="B", markets=m))
    assert f["markets"]["cs"]["0:2"] == 2.25 and f["markets"]["es"]["s3"] == 2.25
    assert model.describe(dict(mkt="cs", line=None, sel="2:0"), "Moutet", "Machac") == "Moutet wins 2 sets to 0"
    assert model.describe(dict(mkt="es", line=None, sel="s3"), "A", "B") == "Match ends in 3 sets"
    assert model.settle(dict(mkt="cs", sel="2:0", odds=2.25), 0, 0, True, (2, 0))[0] == "won"
    assert model.settle(dict(mkt="cs", sel="2:0", odds=2.25), 0, 0, True, (2, 1))[0] == "lost"
    assert model.settle(dict(mkt="es", sel="s3", odds=2.25), 0, 0, True, (2, 1))[0] == "won" and model.settle(dict(mkt="es", sel="s3", odds=2), 0, 0, True) is None
    cat_cs = dict(marketType="correctscore", marketName="Correct Score", period="result", handicap=0, outcomes=[dict(outcomeName=x) for x in ("2:0", "2:1", "0:2", "1:2")])
    cat_b5 = dict(cat_cs, outcomes=[dict(outcomeName=x) for x in ("3:2", "2:3", "1:3", "3:1", "3:0", "0:3")])
    cat_es = dict(marketType="exactsets", marketName="Exact Sets", period="result", handicap=0, outcomes=[dict(outcomeName=x) for x in ("5", "2", "4", "3")])
    c = tpin.classify_tn
    assert c(cat_cs) == ("cs", None) and c(cat_b5) is None and c(cat_es) == ("es", None)
    cs_c = model.candidates(dict(markets=dict(es={"s2": 1.60, "s3": 2.25})), dict(markets=dict(es={"s2": 1.55, "s3": 2.45})))
    assert {c["sel"] for c in cs_c} == {"s2", "s3"} and all(c["mkt"] == "es" for c in cs_c)


def test_only_one_pick_from_the_sets_family_per_match():
    from tennis import model as mm
    assert set(mm.SETS_FAMILY) == {"sh", "es", "cs"}


def test_papi_link_never_pulls_a_tournament_again_while_it_is_fresh():
    from tennis import pin as tp
    t = [1_000_000.0]
    pulls = []

    class Papi:
        calls = 0
        def call(self, path, params=None):
            return []

    pb = tp.PinBook(papi_client=Papi(), load=lambda k, d=None: d, save=lambda k, v: None, clock=lambda: t[0])
    pb.papi_tours = {"t": t[0], "rows": [{"tournamentId": 1}]}
    tours = {"a": [1], "b": [2], "c": [3], "d": [4], "e": [5]}
    pb._tours_for = lambda se: tours[se["id"]]

    def fake_pull(src, reserve=0):
        pulls.append(src)
        pb.src[src] = dict(t=t[0], events=[])
    pb._pull_pp = fake_pull
    games = [dict(id=k, home="A", away="B", t=t[0] + 3600) for k in tours]
    pb.papi_link(games[:4])
    first = len(pulls)
    t[0] += 1300
    pb.nolink_until.clear()
    pb.papi_link(games[1:] + games[:1])               # a different mix of the same tournaments, plus one new
    assert first == 1 and len(pulls) == 2 and pulls[1] == "pp:5"   # only the new tournament is pulled


def test_burn_factor_widens_the_daily_share():
    import os
    from services.oddspapi import OddsPapi
    t = [1_792_800_000.0]
    api = OddsPapi(keys=["a"], get=lambda u, p: ({"request_limit": 250, "request_count": 90}, {}), clock=lambda: t[0], sleep=lambda s: None)
    base = api.daily_allowance()
    os.environ["ODDSPAPI_BURN"] = "1.5"
    try:
        assert abs(api.daily_allowance() - 1.5 * base) < 0.01
    finally:
        os.environ.pop("ODDSPAPI_BURN", None)


def test_unlinked_command_answers_quickly_with_many_tournaments():
    import time as _t
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    from tennis.bot import TennisBot
    pin = eng.pin
    pin.papi_tours = {"t": clock[0], "rows": [{"tournamentId": i, "tournamentName": "City%d Open" % i} for i in range(3000)]}
    calls = []
    pin._tours_for = lambda e: calls.append(1) or [int(e["tname"][4:])]
    feed_events = [dict(id="x%d" % i, lid=str(i), lname="ATP - City%d" % i, tname="City%d" % i, home="A", away="B", t=clock[0] + 3600) for i in range(40)]
    eng.feed.upcoming = lambda h, n=None: feed_events
    eng.priced = lambda e: False
    b = TennisBot(eng)
    t0 = _t.time()
    out = b.cmd_unlinked("")
    assert len(calls) == 40 and _t.time() - t0 < 5


def test_tours_for_skips_doubles_utr_and_the_wrong_gender():
    from tennis import pin as tp
    pb = tp.PinBook(load=lambda k, d=None: d, save=lambda k, v: None, clock=lambda: 1_000_000.0)
    pb.papi_tours = {"t": 1.0, "rows": [{"tournamentId": 1, "tournamentName": "ITF Men Bakersfield, USA"},
                                        {"tournamentId": 2, "tournamentName": "ITF Women Bakersfield, USA"},
                                        {"tournamentId": 3, "tournamentName": "WTA Osaka, Japan"}]}
    assert pb._tours_for(dict(tname="ITF W35 Bakersfield, CA", lname="ITF - ITF W35 Bakersfield, CA")) == [2]
    assert pb._tours_for(dict(tname="ITF M25 Bakersfield", lname="ITF - ITF M25 Bakersfield")) == [1]
    assert pb._tours_for(dict(tname="ATP Challenger Cali, Colombia Men Doubles", lname="Challenger - Cali")) == []
    assert pb._tours_for(dict(tname="UTR PTT Lubbock Men 01", lname="UTR Men - UTR PTT Lubbock Men 01")) == []
    assert pb._tours_for(dict(tname="WTA Osaka, Japan Women Singles", lname="WTA - WTA Osaka")) == [3]


def test_card_table_keeps_the_handicap_line_when_the_name_is_long():
    from tennis import cards
    assert cards._short("Mpetshi Perricard +0.5", 16).endswith("+0.5")
    assert len(cards._short("Mpetshi Perricard +0.5", 16)) <= 16
    assert cards._short("Kovacevic -0.5", 16) == "Kovacevic -0.5"
    assert cards._short("Over 26.5", 16) == "Over 26.5"


def test_set_markets_are_listed_as_not_compared_when_pinnacle_has_no_price():
    from tennis import sporty
    mk = [dict(name="Set Handicap", specifier="hcp=-1.5", outcomes=[]), dict(name="Correct Score", outcomes=[]),
          dict(name="Exact Sets", outcomes=[]), dict(name="Winner", outcomes=[])]
    got = sporty.extras(mk)
    assert "Set handicap" in got and "Exact sets" in got and "Correct score (sets)" in got


def test_card_does_not_list_a_set_market_as_not_compared_when_it_is_in_the_table():
    m = dict(home="A, B", away="C, D", tname="t", lname="l", tip=2_000_000_000.0, picks={}, play_keys=[])
    chk = dict(status="watch", kind="c1", label="Check 1", t=1_999_000_000.0, rows=[], keys=[], pmap={}, away=[], change=[],
               extra=["Set handicap", "Odd / even games"], table=[("SET HANDICAP (closest line)", [("A -1.5", 2.0, 2.0, 0.0)])],
               result="NO BET.", age=1.0, why=[], hint="", held=None, stale=None, nxt=None)
    txt = "\n".join(cards.block(m, chk, True, []))
    assert "not compared" in txt and "Odd / even games" in txt and "Set handicap," not in txt and "compared (no Pinnacle price to check against): Set handicap" not in txt


def test_match_read_sentence_from_the_pinnacle_table():
    tb = [("MATCH WINNER", [("Leong", 1.23, 1.28, -0.039), ("Sun", 3.90, 4.57, -0.146)]),
          ("GAME HANDICAP (closest line)", [("Leong -4.5", 1.88, 1.87, 0.008), ("Sun +4.5", 1.82, 2.16, -0.156)]),
          ("TOTAL GAMES (closest line)", [("Over 20.5", 1.78, 1.88, -0.05), ("Under 20.5", 1.93, 2.14, -0.09)])]
    t = cards.match_read(tb)
    assert "Leong is the favourite, about 78% to win" in t and "margin about 4.5 games" in t and "short, one-sided" in t
    assert cards.match_read([("MATCH WINNER", [("A", 2.0, None, None), ("B", 1.8, None, None)])]) == ""
    close = cards.match_read([("MATCH WINNER", [("A", 1.9, 1.95, 0), ("B", 1.9, 1.95, 0)])])
    assert "coin flip" in close


def test_chances_lists_every_market_in_the_table():
    tb = [("MATCH WINNER", [("Leong", 1.23, 1.28, 0), ("Sun", 3.90, 4.57, 0)]),
          ("SET HANDICAP (closest line)", [("Leong -1.5", 1.6, 1.6, 0), ("Sun +1.5", 2.3, 2.5, 0)]),
          ("EXACT SETS", [("2 sets", 1.5, 1.5, 0), ("3 sets", 2.6, 3.0, 0)]),
          ("TOTAL GAMES (closest line)", [("Over 20.5", 1.78, None, None), ("Under 20.5", 1.93, None, None)])]
    ch = cards.chances(tb)
    assert len(ch) == 3 and ch[0].startswith("Match winner: Leong 78%, Sun 22%") and ch[1].startswith("Set handicap:") and ch[2].startswith("Exact sets: 2 sets 67%, 3 sets 33%")


def test_coverage_check_reads_sportybet_and_pinnacle_and_gives_a_verdict():
    from services import coverage as cv
    now = 1_800_000_000.0
    ev = lambda i, mk: dict(homeTeamName="H%d" % i, awayTeamName="A%d" % i, estimateStartTime=(now + 3600 * (i + 1)) * 1000,
                            markets=[dict(desc=m) for m in mk])
    payload = dict(data=dict(tournaments=[dict(name="NHL", categoryName="USA", events=[ev(i, ["Winner", "Total"]) for i in range(12)]),
                                          dict(name="Czech Extraliga", categoryName="Czechia", events=[ev(i, ["Winner"]) for i in range(3)])]))
    calls = []
    get = lambda url, params, headers, timeout, retries: (calls.append(params["pageNum"]) or (payload if params["pageNum"] == 1 else dict(data=dict(tournaments=[]))), {})

    class Papi:
        calls = 0
        def call(self, path, params=None):
            self.calls += 1
            if path == "/v4/sports":
                return [dict(sportId=9, sportName="Field Hockey"), dict(sportId=15, sportName="Ice Hockey")]
            if path == "/v4/markets":
                return [dict(marketId=101, marketType="moneyline", period="fulltime"), dict(marketId=102, marketType="totals", period="fulltime")]
            if path == "/v4/tournaments":
                assert params["sportId"] == 15
                return [dict(tournamentId=7, tournamentName="NHL"), dict(tournamentId=8, tournamentName="KHL")]
            fx = [dict(tournamentId=7, bookmakerOdds=dict(pinnacle=dict(markets={"101": {}, "102": {}}))) for _ in range(9)]
            return fx
    lines = cv.run(Papi(), get, sports=[("Ice hockey", "sr:sport:4", ("ice hockey", "hockey"))], now=now)
    txt = "\n".join(lines)
    assert "ICE HOCKEY: GOOD FIT" in txt and "15 games" in txt and "1 of the top 2" in txt and "9 fixtures" in txt and "moneyline 9" in txt
    assert "Czech Extraliga" in txt and "No OddsPapi league for" in txt


def test_coverage_sends_explicit_market_ids_and_reports_a_refusal_as_not_checked():
    from services import coverage as cv
    seen = []
    def get(url, params, headers, timeout, retries):
        seen.append(params)
        if not params.get("marketId"):
            raise Exception("HTTP 422: Invalid")
        return dict(data=dict(tournaments=[])), {}
    sb = cv.sporty_scan(get, "sr:sport:4", now=1_800_000_000.0)
    assert seen and all(p["marketId"] for p in seen) and sb["games"] == 0
    def refuse(url, params, headers, timeout, retries):
        raise Exception("HTTP 422: Invalid")
    sb = cv.sporty_scan(refuse, "sr:sport:4", now=1_800_000_000.0)
    assert sb["error"] and cv.verdict(sb, dict(fixtures=0, markets={})).startswith("NOT CHECKED")


def test_tennis_settlement_rows_must_add_up_before_a_pick_is_graded():
    from tennis.engine import Engine
    ok = dict(result=(2, 1), p1=(6, 4), p2=(3, 6), p3=(7, 5))
    got = Engine._pp_parse(ok, False)
    assert got == dict(state="finished", hg=16, ag=15, hs=2, as_=1, home_won=True)
    sw = Engine._pp_parse(ok, True)                                  # SportyBet home = Pinnacle participant 2
    assert sw["hg"] == 15 and sw["ag"] == 16 and sw["hs"] == 1 and sw["as_"] == 2 and sw["home_won"] is False
    assert Engine._pp_parse(dict(result=(1, 0), p1=(6, 4)), False) is None                     # retirement: wait for /result
    assert Engine._pp_parse(dict(result=(2, 0), p1=(6, 4), p2=(3, 6)), False) is None          # set winners disagree with the sets score
    assert Engine._pp_parse(dict(result=(2, 0), p1=(6, 4)), False) is None                     # a set row is missing
    assert Engine._pp_parse({}, False) is None


def test_engine_asks_oddspapi_settlement_for_a_tennis_pick_and_keeps_the_pick_when_unclear():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    asked = []

    class Papi:
        reply = dict(status=dict(statusName="Ended"), settlements=[dict(status="WIN")],
                     scores=dict(result=dict(participant1Score=2, participant2Score=0), p1=dict(participant1Score=6, participant2Score=1),
                                 p2=dict(participant1Score=6, participant2Score=2)))
        def settlement(self, fid):
            asked.append(fid)
            return self.reply
    pn.papi = Papi()
    pn._spent = lambda which, n: None
    clock[0] = 5_000_000.0
    p = dict(pp_id="777", pp_sw=False)
    assert eng._pp_result(p)["hg"] == 12 and eng._pp_result(p) is None and asked == ["777"]
    Papi.reply = dict(Papi.reply, status=dict(statusName="Live"))
    q = dict(pp_id="778")
    assert eng._pp_result(q) is None                                              # not over: nothing graded


def test_sources_probe_reports_reachable_and_failing_feeds():
    from services import sources
    espn = dict(events=[dict(status=dict(type=dict(completed=True)), competitions=[dict(competitors=[dict(team=dict(displayName="Arsenal"), score="2"),
                                                                                                          dict(team=dict(displayName="Chelsea"), score="1")])])])
    db = dict(events=[dict(intHomeScore="3", intAwayScore="1", strHomeTeam="A", strAwayTeam="B"), dict(intHomeScore=None)])

    def get(url, params, headers, timeout, retries):
        if "espn" in url and "tennis/wta" in url:
            raise Exception("HTTP 403: Forbidden")
        return (espn if "espn" in url else db), {}
    txt = "\n".join(sources.probe(get, now=1_800_000_000.0))
    assert "OK   ESPN football England: 1 games, 1 finished, e.g. Arsenal 2 | Chelsea 1" in txt
    assert "FAIL ESPN tennis WTA: HTTP 403" in txt
    assert "OK   TheSportsDB football: 2 games, 1 with a final score, e.g. A 3-1 B" in txt


# ------------------------------------------------------------------ daily board
def test_board_day_runs_six_to_six_wat_and_titles_use_ordinals():
    from tennis import board
    import calendar
    t = calendar.timegm((2026, 10, 28, 4, 30, 0))          # 05:30 WAT is still the 27th's board day
    assert board.board_day(t) == "20261027" and board.date_text(t) == "27th October 2026"
    t = calendar.timegm((2026, 10, 28, 5, 0, 0))           # 06:00 WAT starts the 28th
    assert board.board_day(t) == "20261028" and board.date_text(t) == "28th October 2026"
    lo, hi = board.bracket(t)
    assert lo == t and hi - lo == 86400
    assert [board.ordinal(n) for n in (1, 2, 3, 11, 12, 13, 21, 22, 23, 30)] == ["1st", "2nd", "3rd", "11th", "12th", "13th", "21st", "22nd", "23rd", "30th"]


def test_board_long_list_continues_on_numbered_pages_with_a_way_back():
    from tennis import board
    t = NOW
    games = [dict(id=str(i), lname="League %d" % (i // 20), home="Player %d" % i, away="Other %d" % i, t=t + i * 60) for i in range(150)]
    pages = board.render("pin", t, games, lambda g, n: "✅", limit=1500)
    assert len(pages) > 1 and all(len(p) < 1500 for p in pages)
    assert "(Page 1)" in pages[0] and "(Page 2)" in pages[1] and "Page 1" in pages[1] and "Continues on Page 2" in pages[0]
    assert "Continues" not in pages[-1]
    assert sum(p.count(" vs ") for p in pages) == 150                      # nothing lost across pages
    one = board.render("sporty", t, games[:5], lambda g, n: "✅")
    assert len(one) == 1 and "(Page" not in one[0] and "SportyBet List: " in one[0]


def test_engine_board_posts_three_lists_and_pages_reply_to_the_page_before():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    pn.index = {"tennis_atp_paris": dict(t=NOW, events=[("p1", "Novak Djokovic", "Carlos Alcaraz", ev["t"]), ("p2", "A", "B", ev["t"] + 600)])}
    at(eng, clock, NOW)
    c = eng.cards
    assert {"tn_board:sporty:1", "tn_board:pin:1", "tn_board:both:1"} <= set(c.d)
    assert "Djokovic" in c.d["tn_board:sporty:1"] and "Paris" in c.d["tn_board:pin:1"].replace("Atp Paris", "Paris")
    assert "tracker from" in c.d["tn_board:both:1"] or "👀" in c.d["tn_board:both:1"]


def test_tracker_starts_eight_hours_before_the_match_and_t5_needs_a_bet_signal():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig(tip_in=20 * 3600)
    at(eng, clock, NOW)
    assert not eng.matches                                                  # too early
    clock[0] = NOW + 12 * 3600 + 5
    eng.tick()
    assert len(eng.matches) == 1
    m = list(eng.matches.values())[0]
    last = [s for s in m["slots"] if s["kind"] == "last"][0]
    m["play_keys"] = []
    n = len(sent)
    clock[0] = last["t"] + 1
    eng.tick()
    assert last["done"] and not any("T-5" in x["text"] for x in sent[n:])   # no T-5 card without a signal


def test_extra_lists_watchlist_gaps_and_bets_are_posted():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    other = dict(ev, id="sr:match:10", home="Solo, A", away="Lone, B", t=ev["t"] + 900)
    feed.events[other["id"]] = other                                     # on SportyBet, Pinnacle has nothing
    pn.index = {"tennis_atp_paris": dict(t=NOW, events=[("p1", "Novak Djokovic", "Carlos Alcaraz", ev["t"]), ("p9", "X", "Y", ev["t"] + 300)])}
    at(eng, clock, NOW)
    at(eng, clock, [s for s in list(eng.matches.values())[0]["slots"]][1]["t"] + 1)
    c = eng.cards.d
    assert {"tn_board:watch:1", "tn_board:gaps:1", "tn_board:bets:1"} <= set(c)
    assert "Solo" in c["tn_board:gaps:1"] and "X vs Y" in c["tn_board:gaps:1"] and "Djokovic" not in c["tn_board:gaps:1"]
    assert "Bets and Results: " in c["tn_board:bets:1"]


def test_card_table_rows_use_first_word_and_stay_narrow():
    tb = [("MATCH WINNER", [("Basilashvili", 7.30, 10.78, -0.323), ("Alcaraz", 1.11, 1.10, 0.007)]),
          ("GAME HANDICAP (closest line)", [("Basilashvili +4.5", 2.20, 2.17, 0.014)]),
          ("TOTAL GAMES (closest line)", [("Over 20.5", 1.98, 2.06, -0.04)])]
    rows = [r for blk in cards.table_text(tb) if blk.startswith("<pre>") for r in blk[5:-6].split("\n")]
    assert len(rows) == 4
    assert all(len(r) <= 35 for r in rows)
    assert len({r.index("|") for r in rows}) == 1             # the columns line up
    assert rows[0].startswith("Basilashvili ") and rows[1].startswith("Alcaraz ")
    assert rows[2].endswith("+1.4%") and rows[2].split()[1] == "+4.5"
