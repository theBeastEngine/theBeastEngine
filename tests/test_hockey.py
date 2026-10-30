import json

from hockey import cards, cfg, model, names, pin as hpin, sporty
from hockey.engine import Engine

NOW = 1_800_000_000.0     # fixed clock

SOFT = {"ml": {"home": 2.30, "away": 1.62}, "total": {5.5: {"over": 2.05, "under": 1.80}, 6.5: {"over": 3.2, "under": 1.3}},
        "ah": {-1.5: {"home": 2.30, "away": 1.62}}}
PIN = {"ml": {"home": 2.10, "away": 1.85}, "total": {5.5: {"over": 1.95, "under": 1.90}},
       "ah": {-1.5: {"home": 2.10, "away": 1.80}}}


def test_names_and_qualifiers():
    assert names.same_team("Boston Bruins", "Boston Bruins")
    assert not names.same_team("Dinamo Minsk", "Dinamo Minsk Juniors")
    assert names.bad_league("Russia - MHL") and names.bad_league("eHockey Battle") and not names.bad_league("USA - NHL")


def test_settle_includes_overtime_and_never_grades_a_level_score():
    s = model.settle
    assert s(dict(mkt="ml", line=None, sel="home", odds=2.0), 3, 2) == ("won", 1.0)
    assert s(dict(mkt="ml", line=None, sel="away", odds=2.0), 3, 2) == ("lost", -1.0)
    assert s(dict(mkt="ml", line=None, sel="home", odds=2.0), 2, 2)[0] == "void"
    assert s(dict(mkt="ah", line=-1.5, sel="home", odds=2.0), 4, 2)[0] == "won"
    assert s(dict(mkt="ah", line=-1.5, sel="home", odds=2.0), 3, 2)[0] == "lost"
    assert s(dict(mkt="ah", line=-1.5, sel="away", odds=2.0), 3, 2)[0] == "won"
    assert s(dict(mkt="total", line=5.5, sel="over", odds=1.9), 4, 2)[0] == "won"
    assert s(dict(mkt="total", line=6.0, sel="under", odds=1.9), 4, 2)[0] == "void"


def test_candidates_same_lines_only_and_moneyline_required():
    cs = model.candidates(SOFT, PIN)
    keys = {model.pkey(c) for c in cs}
    assert "total|6.5|over" not in keys                       # Pinnacle has no 6.5: never compared
    assert {"ml||home", "ml||away", "total|5.5|over", "ah|-1.5|home"} <= keys
    best = next(c for c in cs if model.pkey(c) == "ml||home")
    assert best["ev"] > 0.04 and best["why_not"] is None
    no_ml = {k: v for k, v in PIN.items() if k != "ml"}
    assert all(c["why_not"] for c in model.candidates(SOFT, no_ml))
    assert "moneyline" in model.candidates(SOFT, no_ml)[0]["why_not"]


def test_agreement_check_blocks_a_different_game():
    assert model.agree(SOFT, PIN) is None
    assert model.agree(SOFT, {"ml": {"home": 5.0, "away": 1.15}}) is not None


def test_describe():
    assert model.describe(dict(mkt="ah", line=-1.5, sel="away"), "A", "B") == "B +1.5"
    assert model.describe(dict(mkt="total", line=5.5, sel="over"), "A", "B") == "Over 5.5 goals"
    assert "overtime" in model.describe(dict(mkt="ml", line=None, sel="home"), "A", "B")


def test_sporty_parse_uses_only_full_game_markets():
    pl = {"data": {"tournaments": [
        {"id": "sr:tournament:234", "categoryName": "USA", "name": "NHL", "events": [
            {"eventId": "sr:match:1", "homeTeamName": "Boston Bruins", "awayTeamName": "Toronto Maple Leafs", "estimateStartTime": int(NOW * 1000), "markets": [
                {"id": 219, "desc": "Winner (incl. overtime)", "outcomes": [{"id": "4", "desc": "Home", "odds": "2.30"}, {"id": "5", "desc": "Away", "odds": "1.62"}]},
                {"id": 225, "desc": "Total (incl. overtime)", "specifier": "total=5.5", "outcomes": [{"id": "12", "desc": "Over 5.5", "odds": "2.05"}, {"id": "13", "desc": "Under 5.5", "odds": "1.80"}]},
                {"id": 223, "desc": "Handicap (incl. overtime)", "specifier": "hcp=-1.5", "outcomes": [{"id": "1714", "desc": "Home (-1.5)", "odds": "2.30"}, {"id": "1715", "desc": "Away (+1.5)", "odds": "1.62"}]},
                {"id": 1, "desc": "1X2", "outcomes": [{"id": "1", "desc": "Home", "odds": "2.9"}, {"id": "2", "desc": "Draw", "odds": "4.1"}, {"id": "3", "desc": "Away", "odds": "2.2"}]},
                {"id": 18, "desc": "Total", "specifier": "total=5.5", "outcomes": [{"id": "12", "desc": "Over 5.5", "odds": "2.4"}, {"id": "13", "desc": "Under 5.5", "odds": "1.5"}]}]}]},
        {"id": "x", "categoryName": "Russia", "name": "MHL", "events": [
            {"eventId": "2", "homeTeamName": "A", "awayTeamName": "B", "estimateStartTime": int(NOW * 1000)}]}]}}
    evs = sporty.parse_events(pl)
    assert len(evs) == 1 and evs[0]["lname"] == "USA - NHL"
    m = evs[0]["markets"]
    assert m["ml"] == {"home": 2.3, "away": 1.62} and m["total"][5.5]["over"] == 2.05 and m["ah"][-1.5]["away"] == 1.62
    assert set(m) == {"ml", "total", "ah"}                    # the three-way 1X2 and the regulation total are not used
    assert ("1", "1X2") in sporty.SEEN                         # but their names are kept for /status
    bad = sporty.parse_markets([{"id": 219, "desc": "Winner (3-way)", "outcomes": [{"id": "4", "desc": "Home", "odds": "2.3"}, {"id": "5", "desc": "Away", "odds": "1.6"}]}])
    assert bad == {}


def test_normalize_odds_api_skips_three_way_h2h():
    raw = {"id": "e1", "home_team": "Boston Bruins", "away_team": "Toronto Maple Leafs", "commence_time": "2027-01-15T15:00:00Z", "bookmakers": [
        {"key": "pinnacle", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Boston Bruins", "price": 2.1}, {"name": "Toronto Maple Leafs", "price": 1.85}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": 1.95, "point": 5.5}, {"name": "Under", "price": 1.9, "point": 5.5}]},
            {"key": "spreads", "outcomes": [{"name": "Boston Bruins", "price": 2.1, "point": -1.5}, {"name": "Toronto Maple Leafs", "price": 1.8, "point": 1.5}]}]}]}
    ev = hpin.normalize_oa(raw)
    assert ev["markets"]["ml"] == {"home": 2.1, "away": 1.85}
    assert ev["markets"]["total"][5.5] == {"over": 1.95, "under": 1.9}
    assert ev["markets"]["ah"][-1.5] == {"home": 2.1, "away": 1.8}
    raw["bookmakers"][0]["markets"][0]["outcomes"].append({"name": "Draw", "price": 4.2})
    assert "ml" not in hpin.normalize_oa(raw)["markets"]      # a three-way price is regulation time only


def test_classify_papi_hockey_markets():
    c = hpin.classify_fb
    assert c(dict(marketType="moneyline", marketName="Money Line", period="result", outcomes=[1, 2])) == ("ml", None)
    assert c(dict(marketType="moneyline", marketName="Money Line 3-way", period="result", outcomes=[1, 2, 3])) is None
    assert c(dict(marketType="totals", marketName="Over/Under", period="result", handicap=5.5, outcomes=[1, 2])) == ("total", 5.5)
    assert c(dict(marketType="spreads", marketName="Puck Line", period="result", handicap=-1.5, outcomes=[1, 2])) == ("ah", -1.5)
    assert c(dict(marketType="spreads", marketName="Handicap", period="result", handicap=-1.5, outcomes=[1, 2])) == ("ah", -1.5)
    assert c(dict(marketType="totals", marketName="Over/Under", period="p1", handicap=1.5, outcomes=[1, 2])) is None


class FakeFeed:
    def __init__(self, ev):
        self.events = {ev["id"]: ev}
        self.last = {}
        self.today = 0

    def refresh(self, today_only=False, force=False):
        self.today += 1 if today_only else 0
        return 0

    def upcoming(self, h, now=None):
        now = now or NOW
        return [e for e in self.events.values() if now - 600 <= e["t"] <= now + h]


class FakePin:
    def __init__(self, ev):
        self.link = {ev["id"]: ("oa:icehockey_x", "p1")}
        self.papi = None
        self.markets = PIN
        self.age = 3.0
        self.spend = {"oa": 0, "pp": 0}
        self.calls = []

    def refresh_index(self, events, horizon):
        return 0

    def get(self, ev, ttl, force=None):
        self.calls.append(force)
        return dict(id="p1", home=ev["home"], away=ev["away"], t=ev["t"], markets=self.markets), self.age, ""


class FakeResults:
    def __init__(self):
        self.r = None

    def find(self, home, away, t):
        return self.r


class Cards:
    def __init__(self):
        self.d = {}

    def put(self, key, text, day, silent=True, data=None, reply_to=None):
        self.d[key] = text

    def get(self, key):
        return {}

    def _st(self):
        return {}


def rig(tip_in=4 * 3600):
    tip = NOW + tip_in
    ev = dict(id="sr:match:1", lid="L1", lname="USA - NHL", country="USA", tname="NHL",
              home="Boston Bruins", away="Toronto Maple Leafs", t=tip, markets={})
    ev["markets"] = {"ml": dict(SOFT["ml"]), "total": {k: dict(v) for k, v in SOFT["total"].items()}, "ah": {k: dict(v) for k, v in SOFT["ah"].items()}}
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

def at(eng, clock, t):
    clock[0] = t
    eng.tick()

def at(eng, clock, t):
    clock[0] = t
    eng.tick()


def test_two_checks_make_play_now_then_verdicts():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    assert len(sent) == 1 and "NHL" in sent[0]["text"] and not eng.picks["picks"]
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    assert eng.picks["picks"] and any("PLAY NOW" in s["text"] for s in sent)
    for kind in ("t60", "final", "t15", "last"):
        t = next(s["t"] for s in m["slots"] if s["kind"] == kind)
        at(eng, clock, t + 1)
        assert "%s VERDICT" % cards.LATE_NAME[kind] in sent[-1]["text"]


def test_different_game_is_held():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    pn.markets = {"ml": {"home": 5.0, "away": 1.15}}
    at(eng, clock, NOW)
    assert "data check failed" in sent[0]["text"] and not eng.picks["picks"]


def test_grading_uses_final_score():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    clock[0] = ev["t"] + 4 * 3600
    res.r = dict(state="finished", hg=4, ag=3, home="Boston Bruins", away="Toronto Maple Leafs", t=ev["t"])
    assert eng.grade() >= 1
    p = list(eng.picks["picks"].values())[0]
    assert p["status"] in ("won", "lost") and p["score"] == "4-3"


def test_sporty_names_decide_when_ids_differ():
    ms = [{"id": 999, "desc": "Winner (incl. overtime)", "outcomes": [{"id": "4", "desc": "Home", "odds": "1.9"}, {"id": "5", "desc": "Away", "odds": "1.9"}]},
          {"id": 998, "desc": "Total Goals (incl. overtime)", "specifier": "total=5.5", "outcomes": [{"id": "12", "desc": "Over 5.5", "odds": "1.9"}, {"id": "13", "desc": "Under 5.5", "odds": "1.9"}]},
          {"id": 997, "desc": "Handicap (incl. overtime)", "specifier": "hcp=-1.5", "outcomes": [{"id": "1714", "desc": "Home", "odds": "2.5"}, {"id": "1715", "desc": "Away", "odds": "1.5"}]},
          {"id": 996, "desc": "Total Goals", "specifier": "total=5.5", "outcomes": [{"id": "12", "desc": "Over 5.5", "odds": "1.7"}, {"id": "13", "desc": "Under 5.5", "odds": "2.1"}]}]
    m = sporty.parse_markets(ms)
    assert set(m) == {"ml", "total", "ah"} and m["total"][5.5]["over"] == 1.9      # the regulation total (no 'overtime' in its name) is left out


def test_no_bet_line_says_what_each_book_has():
    txt = cards.plain_result([], [], "A", "B", {"ml": {"home": 2, "away": 2}}, {"total": {5.5: {"over": 2, "under": 2}}})
    assert "SportyBet has: moneyline" in txt and "Pinnacle has: total goals" in txt


def test_status_text_shows_markets_seen():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    sporty.parse_markets([{"id": 1, "desc": "1X2", "outcomes": []}])
    text, warn = eng.status_text(NOW)
    assert "HOCKEY ENGINE STATUS" in text and "SportyBet markets seen" in text and "moneyline" in text
