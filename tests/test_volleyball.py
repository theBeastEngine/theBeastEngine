import json

from volleyball import cards, cfg, model, names, pin as vpin, sporty
from volleyball.engine import Engine, sets_ok

NOW = 1_800_000_000.0     # fixed clock

SOFT = {"ml": {"home": 2.30, "away": 1.62}, "total": {3.5: {"over": 2.05, "under": 1.80}, 4.5: {"over": 3.2, "under": 1.3}},
        "ah": {-1.5: {"home": 2.30, "away": 1.62}}}
PIN = {"ml": {"home": 2.10, "away": 1.85}, "total": {3.5: {"over": 1.95, "under": 1.90}},
       "ah": {-1.5: {"home": 2.10, "away": 1.80}}}


def test_names_and_qualifiers():
    assert names.same_team("Vero Volley", "Vero Volley")
    assert not names.same_team("Vero Volley", "Vero Volley Women")
    assert names.bad_league("Italy - Serie A1 Women") and names.bad_league("eVolleyball Battle") and not names.bad_league("Italy - SuperLega")


def test_sets_ok_only_for_real_set_scores():
    assert sets_ok(3, 0) and sets_ok(3, 2) and sets_ok(2, 1) and sets_ok(0, 3)
    assert not sets_ok(25, 20) and not sets_ok(3, 3) and not sets_ok(4, 3) and not sets_ok(1, 1) and not sets_ok(None, 2)


def test_settle_uses_the_set_score():
    s = model.settle
    assert s(dict(mkt="ml", line=None, sel="home", odds=2.0), 3, 1) == ("won", 1.0)
    assert s(dict(mkt="ml", line=None, sel="away", odds=2.0), 3, 1) == ("lost", -1.0)
    assert s(dict(mkt="ah", line=-1.5, sel="home", odds=2.0), 3, 0)[0] == "won"
    assert s(dict(mkt="ah", line=-1.5, sel="home", odds=2.0), 3, 2)[0] == "lost"
    assert s(dict(mkt="ah", line=-1.5, sel="away", odds=2.0), 3, 2)[0] == "won"
    assert s(dict(mkt="total", line=3.5, sel="over", odds=1.9), 3, 1)[0] == "won"
    assert s(dict(mkt="total", line=3.5, sel="under", odds=1.9), 3, 0)[0] == "won"


def test_candidates_same_lines_only_and_moneyline_required():
    cs = model.candidates(SOFT, PIN)
    keys = {model.pkey(c) for c in cs}
    assert "total|4.5|over" not in keys
    assert {"ml||home", "ml||away", "total|3.5|over", "ah|-1.5|home"} <= keys
    assert next(c for c in cs if model.pkey(c) == "ml||home")["why_not"] is None
    no_ml = {k: v for k, v in PIN.items() if k != "ml"}
    assert all(c["why_not"] for c in model.candidates(SOFT, no_ml))


def test_describe():
    assert model.describe(dict(mkt="ah", line=-1.5, sel="away"), "A", "B") == "B +1.5"
    assert model.describe(dict(mkt="total", line=3.5, sel="over"), "A", "B") == "Over 3.5 sets"
    assert model.describe(dict(mkt="ml", line=None, sel="home"), "A", "B") == "A to win the match"


def test_sporty_reads_match_winner_set_handicap_total_sets_only():
    ms = [{"id": 186, "desc": "Winner", "outcomes": [{"id": "4", "desc": "Home", "odds": "1.5"}, {"id": "5", "desc": "Away", "odds": "2.5"}]},
          {"id": 191, "desc": "Handicap (sets)", "specifier": "hcp=-1.5", "outcomes": [{"id": "1714", "desc": "Home (-1.5)", "odds": "2.2"}, {"id": "1715", "desc": "Away (+1.5)", "odds": "1.65"}]},
          {"id": 192, "desc": "Total sets", "specifier": "total=3.5", "outcomes": [{"id": "12", "desc": "Over 3.5", "odds": "1.9"}, {"id": "13", "desc": "Under 3.5", "odds": "1.9"}]},
          {"id": 193, "desc": "Total points", "specifier": "total=180.5", "outcomes": [{"id": "12", "desc": "Over 180.5", "odds": "1.9"}, {"id": "13", "desc": "Under 180.5", "odds": "1.9"}]},
          {"id": 194, "desc": "Set 1 winner", "outcomes": [{"id": "4", "desc": "Home", "odds": "1.8"}, {"id": "5", "desc": "Away", "odds": "1.9"}]},
          {"id": 195, "desc": "1st set - handicap", "specifier": "hcp=-1.5", "outcomes": [{"id": "1714", "desc": "Home", "odds": "1.8"}, {"id": "1715", "desc": "Away", "odds": "1.9"}]},
          {"id": 196, "desc": "Handicap (points)", "specifier": "hcp=-3.5", "outcomes": [{"id": "1714", "desc": "Home", "odds": "1.8"}, {"id": "1715", "desc": "Away", "odds": "1.9"}]}]
    m = sporty.parse_markets(ms)
    assert m == {"ml": {"home": 1.5, "away": 2.5}, "ah": {-1.5: {"home": 2.2, "away": 1.65}}, "total": {3.5: {"over": 1.9, "under": 1.9}}}
    assert ("193", "Total points") in sporty.SEEN


def test_papi_classification_is_sets_only():
    c = vpin.classify_fb
    assert c(dict(marketType="moneyline", marketName="Money Line", period="result", outcomes=[1, 2])) == ("ml", None)
    assert c(dict(marketType="spreads", marketName="Handicap", period="result", handicap=-1.5, outcomes=[1, 2])) == ("ah", -1.5)
    assert c(dict(marketType="spreads", marketName="Handicap", period="result", handicap=-7.5, outcomes=[1, 2])) is None   # a points handicap
    assert c(dict(marketType="totals", marketName="Total sets", period="result", handicap=3.5, outcomes=[1, 2])) == ("total", 3.5)
    assert c(dict(marketType="totals", marketName="Total points", period="result", handicap=180.5, outcomes=[1, 2])) is None
    assert c(dict(marketType="totals", marketName="Over/Under", period="p1", handicap=1.5, outcomes=[1, 2])) is None


def test_agreement_check_blocks_a_different_game():
    assert model.agree(SOFT, PIN) is None
    assert model.agree(SOFT, {"ml": {"home": 5.0, "away": 1.15}}) is not None


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
        self.link = {ev["id"]: ("oa:icevolleyball_x", "p1")}
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
    ev = dict(id="sr:match:1", lid="L1", lname="Italy - SuperLega", country="Italy", tname="SuperLega",
              home="Vero Volley", away="Trentino Itas", t=tip, markets={})
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


def at(eng, clock, t):
    clock[0] = t
    eng.tick()


def test_two_checks_make_play_now_then_verdicts():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    assert len(sent) == 1 and "SuperLega" in sent[0]["text"] and not eng.picks["picks"]
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

def test_grading_only_accepts_a_set_score():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    clock[0] = ev["t"] + 4 * 3600
    res.r = dict(state="finished", hg=25, ag=20, home="Vero Volley", away="Trentino Itas", t=ev["t"])    # a points score: never graded
    assert eng.grade() == 0
    res.r = dict(state="finished", hg=3, ag=1, home="Vero Volley", away="Trentino Itas", t=ev["t"])
    assert eng.grade() >= 1
    p = list(eng.picks["picks"].values())[0]
    assert p["status"] in ("won", "lost") and p["score"] == "3-1"


def test_status_text_lists_markets_seen():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    text, warn = eng.status_text(NOW)
    assert "VOLLEYBALL ENGINE STATUS" in text and "SportyBet markets seen" in text and "moneyline" in text


def test_probe_never_raises_and_reports():
    def boom(*a, **k):
        raise RuntimeError("x")
    f = sporty.SportyFeed(get=boom)
    f.probe(NOW)
    assert f.last["probe"].startswith("failed")
    assert "186" in sporty.MARKET_IDS and "260" in sporty.MARKET_IDS
