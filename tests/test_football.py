import json
import time

import pytest

from football import cards, cfg, model, names, pin as fpin, results, sporty
from football.engine import Engine

NOW = 1_800_000_000.0     # fixed clock


# ------------------------------------------------------------------ names
def test_names_match_and_qualifiers():
    assert names.same_team("Manchester United", "Man Utd")
    assert names.same_team("Bayern Munich", "FC Bayern Muenchen")
    assert not names.same_team("Arsenal", "Arsenal W")
    assert not names.same_team("Barcelona", "Barcelona B")
    assert not names.same_team("Inter", "Inter U19")
    assert not names.same_team("Real Madrid", "Real Sociedad")
    assert names.pair_score("Arsenal", "Chelsea", "Chelsea", "Arsenal") == 0.0           # home / away swapped is another game
    assert names.bad_league("England - Premier League Women") and names.bad_league("eSoccer Battle") and not names.bad_league("England - Premier League")


# ------------------------------------------------------------------ model
def test_settle_whole_half_and_quarter_lines():
    s = model.settle
    assert s(dict(mkt="ah", line=-0.25, sel="home", odds=2.0), 1, 0) == ("won", 1.0)
    assert s(dict(mkt="ah", line=-0.25, sel="home", odds=2.0), 0, 0) == ("halflost", -0.5)
    assert s(dict(mkt="ah", line=-0.75, sel="home", odds=2.0), 1, 0) == ("halfwon", 0.5)
    assert s(dict(mkt="ah", line=0.0, sel="away", odds=2.0), 1, 1) == ("void", 0.0)
    assert s(dict(mkt="total", line=2.5, sel="over", odds=1.9), 2, 1)[0] == "won"
    assert s(dict(mkt="total", line=2.0, sel="under", odds=1.9), 1, 1)[0] == "void"
    assert s(dict(mkt="1x2", line=None, sel="draw", odds=3.4), 1, 1)[0] == "won"
    assert s(dict(mkt="1x2", line=None, sel="home", odds=2.0), 0, 1) == ("lost", -1.0)


SOFT = {"1x2": {"home": 2.30, "draw": 3.40, "away": 3.20}, "total": {2.5: {"over": 2.05, "under": 1.80}, 3.5: {"over": 3.2, "under": 1.3}},
        "ah": {-0.5: {"home": 2.30, "away": 1.62}}}
PIN = {"1x2": {"home": 2.10, "draw": 3.50, "away": 3.60}, "total": {2.5: {"over": 1.95, "under": 1.90}},
       "ah": {-0.5: {"home": 2.10, "away": 1.80}}}


def test_candidates_only_identical_lines_and_gates():
    cs = model.candidates(SOFT, PIN)
    keys = {model.pkey(c) for c in cs}
    assert "total|3.5|over" not in keys                      # Pinnacle has no 3.5: never compared
    assert "total|2.5|over" in keys and "ah|-0.5|home" in keys and "1x2||home" in keys
    best = next(c for c in cs if model.pkey(c) == "ah|-0.5|home")
    assert best["ev"] > 0.04 and best["why_not"] is None
    assert next(c for c in cs if model.pkey(c) == "1x2||draw")["why_not"]       # draws switched off


def test_agreement_check_blocks_a_different_game():
    assert model.agree(SOFT, PIN) is None
    other = {"1x2": {"home": 6.0, "draw": 4.0, "away": 1.5}}
    assert "disagree" in model.agree(SOFT, other) or "opposite" in model.agree(SOFT, other)


# ------------------------------------------------------------------ SportyBet parse
def test_sporty_parse_markets_and_exclusions():
    pl = {"data": {"tournaments": [
        {"id": "sr:tournament:17", "categoryName": "England", "name": "Premier League", "events": [
            {"eventId": "sr:match:1", "homeTeamName": "Arsenal", "awayTeamName": "Chelsea", "estimateStartTime": int(NOW * 1000), "markets": [
                {"id": 1, "outcomes": [{"id": "1", "desc": "Home", "odds": "2.30"}, {"id": "2", "desc": "Draw", "odds": "3.40"}, {"id": "3", "desc": "Away", "odds": "3.20"}]},
                {"id": 18, "specifier": "total=2.5", "outcomes": [{"id": "12", "desc": "Over 2.5", "odds": "2.05"}, {"id": "13", "desc": "Under 2.5", "odds": "1.80"}]},
                {"id": 16, "specifier": "hcp=-0.5", "outcomes": [{"id": "1714", "desc": "Home (-0.5)", "odds": "2.30"}, {"id": "1715", "desc": "Away (+0.5)", "odds": "1.62"}]},
                {"id": 11, "outcomes": [{"id": "4", "desc": "Home", "odds": "1.60"}, {"id": "5", "desc": "Away", "odds": "2.30", "isActive": 0}]}]}]},
        {"id": "x", "categoryName": "England", "name": "Premier League 2 Women", "events": [
            {"eventId": "2", "homeTeamName": "A", "awayTeamName": "B", "estimateStartTime": int(NOW * 1000)}]}]}}
    evs = sporty.parse_events(pl)
    assert len(evs) == 1 and evs[0]["lname"] == "England - Premier League"
    m = evs[0]["markets"]
    assert m["1x2"] == {"home": 2.3, "draw": 3.4, "away": 3.2} and m["total"][2.5]["over"] == 2.05 and m["ah"][-0.5]["away"] == 1.62
    assert 0.0 not in m["ah"]                                 # a draw-no-bet with a suspended side is not used


# ------------------------------------------------------------------ Pinnacle normalize
def test_normalize_odds_api_event():
    raw = {"id": "e1", "home_team": "Arsenal", "away_team": "Chelsea", "commence_time": "2027-01-15T15:00:00Z", "bookmakers": [
        {"key": "pinnacle", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 2.1}, {"name": "Chelsea", "price": 3.6}, {"name": "Draw", "price": 3.5}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": 1.95, "point": 2.5}, {"name": "Under", "price": 1.9, "point": 2.5}]},
            {"key": "spreads", "outcomes": [{"name": "Arsenal", "price": 2.1, "point": -0.5}, {"name": "Chelsea", "price": 1.8, "point": 0.5}]}]},
        {"key": "other", "markets": [{"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 9.9}]}]}]}
    ev = fpin.normalize_oa(raw)
    assert ev["markets"]["1x2"] == {"home": 2.1, "away": 3.6, "draw": 3.5}
    assert ev["markets"]["total"][2.5] == {"over": 1.95, "under": 1.9}
    assert ev["markets"]["ah"][-0.5] == {"home": 2.1, "away": 1.8}


def test_classify_papi_football_markets():
    c = fpin.classify_fb
    assert c(dict(marketType="1x2", marketName="Full Time Result", period="result", outcomes=[1, 2, 3])) == ("1x2", None)
    assert c(dict(marketType="totals", marketName="Over/Under", period="result", handicap=2.5, outcomes=[1, 2])) == ("total", 2.5)
    assert c(dict(marketType="spreads", marketName="Asian Handicap", period="result", handicap=-0.75, outcomes=[1, 2])) == ("ah", -0.75)
    assert c(dict(marketType="dnb", marketName="Draw No Bet", period="result", outcomes=[1, 2])) == ("ah", 0.0)
    assert c(dict(marketType="totals", marketName="Corners Over/Under", period="result", handicap=9.5, outcomes=[1, 2])) is None
    assert c(dict(marketType="totals", marketName="Over/Under", period="p1", handicap=1.5, outcomes=[1, 2])) is None   # first half
    assert c(dict(marketType="totals", marketName="Team Total Home", period="result", handicap=1.5, outcomes=[1, 2])) is None


# ------------------------------------------------------------------ engine end to end
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
        self.link = {ev["id"]: ("oa:soccer_x", "p1")}
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
    ev = dict(id="sr:match:1", lid="L1", lname="England - Premier League", country="England", tname="Premier League",
              home="Arsenal", away="Chelsea", t=tip, markets=json.loads(json.dumps({"1x2": SOFT["1x2"]})))
    ev["markets"] = {"1x2": dict(SOFT["1x2"]), "total": {k: dict(v) for k, v in SOFT["total"].items()}, "ah": {k: dict(v) for k, v in SOFT["ah"].items()}}
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


def test_two_checks_make_play_now_then_verdicts_and_summary():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    tip = ev["t"]
    at(eng, clock, NOW)                                              # discovery + check 1: edge seen, not yet confirmed
    assert len(sent) == 1 and sent[0]["silent"] is True and "Premier League" in sent[0]["text"]
    assert not eng.picks["picks"]
    assert "waiting for a second check" in sent[0]["text"]
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)                           # check 2 agrees: PLAY NOW + audible ping
    assert eng.picks["picks"] and any("PLAY NOW" in s["text"] for s in sent)
    p = list(eng.picks["picks"].values())[0]
    assert p["status"] == "new" and p["min_odds"] > 1.0
    for kind in ("t60", "final", "t15", "last"):
        t = next(s["t"] for s in m["slots"] if s["kind"] == kind)
        at(eng, clock, t + 1)
        assert "%s VERDICT" % cards.LATE_NAME[kind] in sent[-1]["text"] and sent[-1]["silent"] is False and sent[-1]["reply_to"] == 101
    assert "BOTTOM LINE: play 2 of 2" in sent[-1]["text"]
    txt = edits[-1]["text"]
    assert "SUMMARY" in txt and "T-60: <b>Play</b>" in txt and "T-5: <b>Play</b>" in txt and "Markets to take" in txt
    assert pn.calls[-4:] == [8.0] * 4                  # late checks force a fresh Pinnacle pull
    assert len(eng.cplog._d()) == 2 and set(list(eng.cplog._d().values())[0]["v"]) == {"t60", "final", "t15", "last"}


def test_stale_pinnacle_price_is_never_confirmed():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    pn.age = 40.0
    t = next(s["t"] for s in m["slots"] if s["kind"] == "t60")
    at(eng, clock, t + 1)
    assert "do not play on this check" in sent[-1]["text"] and "NOT CONFIRMED" in sent[-1]["text"]


def test_edge_gone_is_a_skip():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    pn.markets = {"1x2": dict(SOFT["1x2"]), "total": {2.5: {"over": 2.05, "under": 1.80}}, "ah": {-0.5: dict(SOFT["ah"][-0.5])}}   # Pinnacle now equals SportyBet
    t = next(s["t"] for s in m["slots"] if s["kind"] == "final")
    at(eng, clock, t + 1)
    assert "SKIP" in sent[-1]["text"] and "skip this match" in sent[-1]["text"]


def test_different_game_is_held_by_the_data_check():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    pn.markets = {"1x2": {"home": 6.0, "draw": 4.0, "away": 1.5}}
    at(eng, clock, NOW)
    assert "data check failed" in sent[0]["text"] and not eng.picks["picks"]


def test_unpriced_game_gets_no_card_but_is_on_the_agenda():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    pn.link = {}
    at(eng, clock, NOW + 9 * 3600 - 9 * 3600 + 8 * 3600)            # any time: no card for an unpriced game
    assert not sent and not eng.matches


def test_grading_and_report():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    pid = list(eng.picks["picks"])[0]
    clock[0] = ev["t"] + 3 * 3600
    assert eng.grade() == 0                                          # no final score yet: stays open
    res.r = dict(state="finished", hg=2, ag=1, home="Arsenal", away="Chelsea", t=ev["t"])
    assert eng.grade() == 2
    p = eng.picks["picks"][pid]
    assert p["status"] in ("won", "lost", "halfwon", "halflost") and p["score"] == "2-1" and plain
    assert eng.stats()["graded"] == 2
    assert eng.result_of_pick(pid) in ("won", "lost")


def test_toggle_placed_and_skip():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    pid = int(list(eng.picks["picks"])[0])
    assert "placed at" in eng.toggle(pid) and eng.pick(pid)["status"] == "placed"
    assert eng.toggle(pid) == "Not placed"
    assert eng.skip_match(m["key"]) is True


def test_results_parse_uses_normaltime():
    r = results.parse({"events": [{"homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}, "homeScore": {"current": 3, "normaltime": 2},
                                   "awayScore": {"current": 3, "normaltime": 2}, "status": {"type": "finished"}, "startTimestamp": 5},
                                  {"homeTeam": {"name": "C"}, "awayTeam": {"name": "D"}, "status": {"type": "postponed"}, "startTimestamp": 6}]})
    assert r[0]["hg"] == 2 and r[0]["state"] == "finished" and r[1]["state"] == "void"


def test_bot_commands_and_callbacks():
    from football.bot import FootballBot
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    bot = FootballBot(eng)
    for cmd in ("/status", "/open", "/bets", "/winrate", "/bankroll", "/checkpoints", "/today", "/agenda", "/leagues", "/keys", "/help", "hello"):
        out = bot.handle_text(cmd)
        assert out and "Error" not in str(out), (cmd, out)
    toast, kb = bot.handle_callback("ptog:1:1,2")
    assert toast and kb["inline_keyboard"][0][0]["text"].startswith("\u2705")
    assert bot.handle_callback("mskip:%s" % m["key"])[0].startswith("Match skipped")


def test_grading_from_odds_api_scores_by_event_id():
    from football.results import Results

    class OA:
        def scores(self, key, days):
            return [{"id": "g1", "completed": True, "home_team": "Arsenal", "away_team": "Chelsea",
                     "scores": [{"name": "Arsenal", "score": "2"}, {"name": "Chelsea", "score": "1"}]},
                    {"id": "g2", "completed": False, "home_team": "A", "away_team": "B", "scores": None}]
    r = Results(oa=OA())
    assert r.find_oa(dict(oa_key="soccer_epl", oa_id="g1")) == dict(state="finished", hg=2, ag=1)
    assert r.find_oa(dict(oa_key="soccer_epl", oa_id="g2")) is None and r.find_oa(dict(oa_key="soccer_epl", oa_id="nope")) is None


def test_engine_grades_through_find_oa_and_flags_stuck_picks():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    at(eng, clock, NOW)
    m = list(eng.matches.values())[0]
    at(eng, clock, m["slots"][1]["t"] + 1)
    clock[0] = ev["t"] + 10 * 3600
    assert "could not be graded automatically" in eng.status_text(clock[0])[0]          # no result source answered: the person is told
    res.find_oa = lambda p: dict(state="finished", hg=2, ag=1)
    assert eng.grade() == 2 and eng.stats()["graded"] == 2


def test_watch_card_shows_the_closest_market_table_in_plain_words():
    from football import cards, model
    soft = dict(markets={"1x2": {"home": 2.10, "draw": 3.40, "away": 3.60}, "total": {2.5: {"over": 1.90, "under": 1.95}}})
    pin = dict(markets={"1x2": {"home": 2.05, "draw": 3.45, "away": 3.70}, "total": {2.5: {"over": 1.92, "under": 1.92}}})
    cs = model.candidates(soft, pin)
    txt = cards.plain_result(cs, [c for c in cs if c["why_not"] is None], "Arsenal", "Chelsea")
    assert txt.startswith("NO BET. Closest to a bet:") and "points short of the" in txt
    tb = cards.table_text(cards.compare_table(cs, "Arsenal", "Chelsea", None))
    assert any("MATCH RESULT" in x for x in tb) and any("TOTAL GOALS" in x for x in tb) and any("Draw" in x for x in tb)


def test_oddspapi_settlement_summary_only_trusts_a_finished_documented_reply():
    from services import oddspapi as op
    done = dict(status=dict(statusName="Ended"), scores=dict(result=dict(participant1Score=2, participant2Score=1), fulltime=dict(participant1Score=2, participant2Score=1)),
                settlements=[dict(status="WIN"), dict(status="LOSE"), dict(status="UNDECIDED")])
    assert op.settlement_summary(done) == dict(state="finished", rows=dict(result=(2, 1), fulltime=(2, 1)))
    live = dict(done, status=dict(statusName="Live"))
    assert op.settlement_summary(live) is None                                   # half-time markets can settle while the game is on
    assert op.settlement_summary(dict(done, settlements=[dict(status="UNDECIDED")])) is None
    assert op.settlement_summary(dict(scores={}, settlements=[dict(status="CANCELLED")])) == dict(state="void", rows={})
    assert op.settlement_summary("nonsense") is None and op.settlement_summary({}) is None


def test_engine_grades_an_oddspapi_priced_pick_from_settlement_and_asks_at_most_every_20_minutes():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    asked = []

    class Papi:
        def settlement(self, fid):
            asked.append(fid)
            return dict(status=dict(statusName="Ended"), scores=dict(fulltime=dict(participant1Score=3, participant2Score=0)),
                        settlements=[dict(status="WIN")])
    pn.papi = Papi()
    pn._spent = lambda which, n: None
    clock[0] = 5_000_000.0
    p = dict(pp_id="12345")
    assert eng._pp_result(p) == dict(state="finished", hg=3, ag=0) and asked == ["12345"]
    assert eng._pp_result(p) is None and asked == ["12345"]                      # asked a minute ago: not again yet
    clock[0] += 1300
    assert eng._pp_result(p)["hg"] == 3 and len(asked) == 2
    assert eng._pp_result(dict()) is None                                        # a pick not priced through OddsPapi


def test_a_refused_result_source_is_not_asked_again_for_hours():
    from football.results import Results
    from services.client import HttpError
    t = [1000.0]
    calls = []

    def get(url, params, headers, timeout, retries):
        calls.append(url)
        raise HttpError(403, "Forbidden")
    r = Results(get=get, clock=lambda: t[0])
    assert r.day("2026-10-24") == [] and r.day("2026-10-23") == [] and len(calls) == 1
    t[0] += 7 * 3600
    r.day("2026-10-25")
    assert len(calls) == 2


def test_team_and_player_markets_are_never_read_as_match_totals_or_handicaps():
    from football import pin
    from tennis import pin as tp
    outs = [dict(outcomeId=1, outcomeName="Over"), dict(outcomeId=2, outcomeName="Under")]
    mk = lambda mt, mn, h=1.5: dict(marketType=mt, marketName=mn, period="result", handicap=h, outcomes=outs)
    for mt, mn in [("teamtotals-team1", "Team 1 Total Goals"), ("teamtotals-team2", "Team 2 Total"), ("teamtotals", "Home Total"),
                   ("playertotals-shots", "Player Total Shots"), ("spreads-team1", "Team 1 Handicap")]:
        assert pin.classify_fb(mk(mt, mn)) is None, (mt, mn)
    assert pin.classify_fb(mk("totals", "Total Goals")) == ("total", 1.5)
    assert pin.classify_fb(mk("spreads", "Asian Handicap", -0.5)) == ("ah", -0.5)
    assert tp.classify_tn(mk("teamtotals-team1", "Team 1 Total Games", 11.5)) is None
    assert tp.classify_tn(mk("totals", "Total Games", 22.5)) == ("total", 22.5)
    assert tp.classify_tn(mk("spreads-games", "Game Handicap", -3.5)) == ("ah", -3.5)


def test_football_card_lists_pinnacle_chances_for_every_market_and_a_plain_read():
    from football import cards
    tb = [("MATCH RESULT (1X2)", [("Arsenal", 1.60, 1.65, 0), ("Draw", 4.2, 4.0, 0), ("Chelsea", 5.5, 5.2, 0)]),
          ("ASIAN HANDICAP (closest line)", [("Arsenal -1", 2.1, 2.05, 0), ("Chelsea +1", 1.8, 1.85, 0)]),
          ("TOTAL GOALS (closest line)", [("Over 2.5", 1.9, 1.95, 0), ("Under 2.5", 1.9, 1.9, 0)])]
    ch = cards.chances(tb)
    assert len(ch) == 3 and ch[0].startswith("Match result: Arsenal 58%") and ch[2].startswith("Total goals:")
    rd = cards.match_read(tb)
    assert "Arsenal is the favourite, about 58% to win; a draw is about" in rd and "normal number of goals" in rd
    assert cards.match_read([("MATCH RESULT (1X2)", [("A", 2.0, None, None), ("D", 3.0, None, None), ("B", 3.0, None, None)])]) == ""


def test_football_board_posts_the_six_lists_and_tracker_starts_eight_hours_out():
    eng, feed, pn, res, sent, edits, plain, clock, ev = rig()
    pn.index = {"soccer_epl": dict(t=NOW, events=[("p1", "Arsenal", "Chelsea", ev["t"]), ("p2", "X FC", "Y FC", ev["t"] + 600)])}
    other = dict(ev, id="sr:match:2", home="Solo FC", away="Lone FC", t=ev["t"] + 900)
    feed.events[other["id"]] = other
    at(eng, clock, NOW)
    c = eng.cards.d
    assert {"fb_board:sporty:1", "fb_board:pin:1", "fb_board:both:1", "fb_board:watch:1", "fb_board:gaps:1", "fb_board:bets:1"} <= set(c)
    assert "Arsenal" in c["fb_board:sporty:1"] and "Premier League" not in c["fb_board:pin:1"] or "Epl" in c["fb_board:pin:1"]
    assert "Solo FC" in c["fb_board:gaps:1"] and "X FC vs Y FC" in c["fb_board:gaps:1"]
    far, f2, p2, r2, s2, e2, pl2, clk2, ev2 = rig(tip_in=20 * 3600)
    at(far, clk2, NOW)
    assert not far.matches                                            # too early: tracker starts 8 hours before
    clk2[0] = NOW + 12 * 3600 + 5
    far.tick()
    assert len(far.matches) == 1


def test_zero_handicap_line_is_printed_once_on_the_card():
    from football import cards, model
    soft = dict(markets={"1x2": {"home": 2.55, "draw": 3.50, "away": 2.70}, "ah": {0.0: {"home": 1.83, "away": 1.95}}})
    pin = dict(markets={"1x2": {"home": 2.56, "draw": 3.66, "away": 2.98}, "ah": {0.0: {"home": 1.85, "away": 2.18}}})
    cs = model.candidates(soft, pin)
    tb = cards.compare_table(cs, "Atletico Mineiro MG", "Cruzeiro EC MG", None)
    labels = [lab for head, rows in tb if head.startswith("ASIAN") for lab, *_ in rows]
    assert labels and all(lab.endswith(" 0") and not lab.endswith(" 0 0") for lab in labels), labels
    assert not any(" 0 0" in x for x in cards.chances(tb))
