"""13 Oct build: 2nd-half and Q4 markets, overtime rule, Pinnacle merge, 404 cache, per-match order, live stance lock,
pre-match position tracker, quarter-phase messages, tier, urgent alert format. Pure logic: no database, no network."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from config.markets import BY_KEY
from core import gates, stance, positions, quarters
from core.bookmakers import sportybet as sb
from core.bookmakers.soft_feed import rows_to_events
from services import oddspapi as op


# ---------------------------------------------------------------- markets / periods
def _cat(mid, mtype, period, name, hcap, outs):
    return dict(marketId=mid, marketName=name, marketType=mtype, period=period, handicap=hcap,
                outcomes=[dict(outcomeId=i, outcomeName=n) for i, n in outs])


def test_period_codes_second_half_and_overtime_variants():
    t = [(1, "Over"), (2, "Under")]
    assert op.period_code(_cat(1, "totals", "p3+p4", "2nd Half Total", 80.5, t)) == "h2"
    assert op.period_code(_cat(2, "spreads", "p4+overtime", "Q4 Spread incl. overtime", -1.5, [(1, "1"), (2, "2")])) == "q4ot"
    assert op.period_code(_cat(3, "spreads", "p3+p4+overtime", "2H Spread incl. overtime", -1.5, [(1, "1"), (2, "2")])) == "h2ot"
    assert op.period_code(_cat(1, "totals", "p3+p4", "x", 1, t), n_periods=2) is None


def test_normalize_totals_h2_and_overtime_spreads_but_no_regulation_q4_spread():
    def m(prices):
        return dict(outcomes={str(o): dict(players={"0": dict(active=True, price=p)}) for o, p in prices.items()})
    cat = {
        1: _cat(1, "totals", "p3+p4", "2nd Half Total 80.5", 80.5, [(11, "Over"), (12, "Under")]),
        2: _cat(2, "totals", "p4", "Q4 Total 40.5", 40.5, [(21, "Over"), (22, "Under")]),
        3: _cat(3, "spreads", "p4", "Q4 Spread -1.5", -1.5, [(31, "1"), (32, "2")]),          # must be ignored
        4: _cat(4, "spreads", "p4+overtime", "Q4 Spread -1.5 incl. overtime", -1.5, [(41, "1"), (42, "2")]),
        5: _cat(5, "totals", "p4+overtime", "Q4 Total incl. overtime", 40.5, [(51, "Over"), (52, "Under")]),  # must be ignored
    }
    fx = dict(fixtureId="f1", participant1Id=1, participant2Id=2, startTime="2099-01-01T19:00:00.000Z",
              bookmakerOdds={"pinnacle": dict(markets={
                  "1": m({11: 1.9, 12: 1.9}), "2": m({21: 1.9, 22: 1.9}), "3": m({31: 1.9, 32: 1.9}),
                  "4": m({41: 1.9, 42: 1.9}), "5": m({51: 1.9, 52: 1.9})})})
    ev = op.normalize([fx], {"1": "A", "2": "B"}, cat, "nba", {})[0]
    assert set(ev["markets"]) == {"totals_h2", "totals_q4", "spreads_q4ot"}
    assert gates.kind_period({"market": "totals_h2"}) == ("total", "h2")
    assert gates.kind_period({"market": "spreads_q4ot"}) == ("spread", "q4")
    assert "totals_h2" in BY_KEY and "spreads_h2ot" in BY_KEY


def _out(i, desc, odds):
    return {"id": str(i), "desc": desc, "odds": str(odds), "isActive": 1}


def _mk(mid, spec, outs, name=""):
    return {"id": mid, "specifier": spec, "status": 0, "outcomes": outs, "name": name}


def _payload(markets):
    return {"data": {"tournaments": [{"categoryName": "USA", "name": "NBA", "events": [{
        "eventId": "sr:match:1", "homeTeamName": "Lakers", "awayTeamName": "Celtics",
        "estimateStartTime": 4102444800000, "markets": markets}]}]}}


def test_sportybet_second_half_and_overtime_label_decide_the_key():
    full = [_mk(219, None, [_out(4, "Home", 1.5), _out(5, "Away", 2.6)])]
    mk = full + [
        _mk(90, "total=110.5", [_out(12, "Over 110.5", 1.9), _out(13, "Under 110.5", 1.9)], "2nd half total"),
        _mk(88, "hcp=-1.5", [_out(1714, "Home (-1.5)", 1.9), _out(1715, "Away (+1.5)", 1.9)], "2nd half handicap (incl. overtime)"),
        _mk(236, "total=55.5|quarternr=4", [_out(12, "Over 55.5", 1.9), _out(13, "Under 55.5", 1.9)], "Total 4th quarter"),
        _mk(303, "hcp=-1.5|quarternr=4", [_out(1714, "Home (-1.5)", 1.9), _out(1715, "Away (+1.5)", 1.9)],
            "Handicap 4th quarter (incl. overtime)")]
    rows = sb.parse_events(_payload(mk))
    markets = {r["market"] for r in rows}
    assert {"Total_h2", "Spread_h2ot", "Total_q4", "Spread_q4ot"} <= markets
    assert "Spread_h2" not in markets and "Spread_q4" not in markets
    ev = rows_to_events(rows, "nba")[0]
    assert {"totals_h2", "spreads_h2ot", "totals_q4", "spreads_q4ot"} <= set(ev["markets"])


def test_regulation_label_missing_keeps_total_regulation_and_overtime_total_never_matches():
    mk = [_mk(219, None, [_out(4, "Home", 1.5), _out(5, "Away", 2.6)]),
          _mk(90, "total=110.5", [_out(12, "Over 110.5", 1.9), _out(13, "Under 110.5", 1.9)], "2nd half total (incl. overtime)")]
    rows = sb.parse_events(_payload(mk))
    assert {r["market"] for r in rows} >= {"Total_h2ot"} and "Total_h2" not in {r["market"] for r in rows}


# ---------------------------------------------------------------- send order
def test_send_order_full_game_then_q1_q2_h1_q3_q4_h2_total_before_spread():
    mkts = ["totals_h2", "spreads_q3", "totals_q4", "spreads_h1", "totals_q1", "spreads_q2", "totals", "spreads", "h2h",
            "spreads_q1", "totals_h1", "totals_q3"]
    got = [p["market"] for p in sorted((dict(market=m) for m in mkts), key=gates.send_order)]
    assert got == ["h2h", "spreads", "totals", "totals_q1", "spreads_q1", "spreads_q2", "totals_h1", "spreads_h1",
                   "totals_q3", "spreads_q3", "totals_q4", "totals_h2"]


def test_scan_cap_no_longer_limits_a_slate():
    assert gates.scan_cap("prematch") >= 200


# ---------------------------------------------------------------- pinref merge + 404 cache
def _pin_event(markets, commence, eid="x"):
    return dict(event_id=eid, league="nba", home="Los Angeles Lakers", away="Boston Celtics",
                commence=commence.isoformat(), markets=markets)


def _tot(line, o=1.9, u=1.9):
    return [dict(book="pinnacle", selection="over", line=line, odds=o), dict(book="pinnacle", selection="under", line=line, odds=u)]


def test_match_merges_period_markets_from_second_source_main_wins_duplicates():
    from core.pinref import PinRef
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    c = now + timedelta(hours=20)
    ref = PinRef(api=None, path=os.path.join(tempfile.mkdtemp(), "p.json"))
    stamp = now.isoformat()
    ref.data["leagues"]["nba"] = dict(fetched_at=stamp, events=[_pin_event({"totals": _tot(220.5)}, c, "main")])
    ref.data["extras"] = {"nba": dict(fetched_at=stamp, events=[_pin_event({"totals": _tot(999.5), "totals_q1": _tot(55.5)}, c, "papi")])}
    m = ref.match(dict(league="nba", home="LA Lakers", away="Boston Celtics", commence=c))
    assert set(m["markets"]) == {"totals", "totals_q1"}
    assert m["markets"]["totals"][0]["line"] == 220.5            # the main source is never overwritten
    assert set(ref.data["leagues"]["nba"]["events"][0]["markets"]) == {"totals"}   # cache itself untouched


def test_oddspapi_404_is_an_empty_answer_with_a_short_ttl(monkeypatch):
    from core.pinref import PinRef
    from services.client import HttpError

    class C:
        calls = 0

        def catalog(self):
            return {}

        def participants(self):
            return {}

        def odds(self, tids):
            self.calls += 1
            raise HttpError(404, "FIXTURE_NOT_FOUND")
    monkeypatch.setenv("ODDSPAPI_KEYS", "k")
    ref = PinRef(api=None, path=os.path.join(tempfile.mkdtemp(), "p.json"))
    ref._papi = C()
    assert ref._refresh_papi("acb") == 0
    blob = ref.data["leagues"]["acb"]
    assert blob["events"] == [] and blob["ttl_min"] == 150 and blob["raw_fixtures"] == 0
    assert ref.ensure_fresh("acb") is True and ref._papi.calls == 1       # not asked again inside the ttl


def test_near_tip_refresh_is_faster():
    from core.pinref import PinRef
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ref = PinRef(api=None, path=os.path.join(tempfile.mkdtemp(), "p.json"))
    soon = dict(events=[dict(commence=(now + timedelta(hours=1)).isoformat())])
    later = dict(events=[dict(commence=(now + timedelta(hours=30)).isoformat())])
    assert ref._ttl_min(soon) < ref._ttl_min(later)
    assert ref.refresh_id("nba") == "|"


# ---------------------------------------------------------------- live stance
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, s=120.0):
        self.t += s


def _lp(sel="home", market="spreads", ev=0.06, p=0.62, period=1, hs=20, as_=18, line=-3.5, eid="e1"):
    return dict(league="nba", event_id=eid, home="Lakers", away="Celtics", market=market, selection=sel, side=sel, line=line,
                ev=ev, p=p, period=period, home_score=hs, away_score=as_)


def test_stable_read_needed_before_the_first_alert():
    clk, bk = Clock(), None
    bk = stance.StanceBook(clock=clk)
    send, held = stance.gate(bk, [_lp()])
    assert not send and held[0]["stance_decision"]["action"] == "hold"
    clk.tick()
    send, held = stance.gate(bk, [_lp()])
    assert len(send) == 1 and send[0]["stance_decision"]["reversal"] is False


def test_changing_side_resets_the_count():
    clk = Clock()
    bk = stance.StanceBook(clock=clk)
    stance.gate(bk, [_lp("home")])
    clk.tick()
    send, held = stance.gate(bk, [_lp("away")])
    assert not send                       # the best side changed: starts again
    clk.tick()
    send, _ = stance.gate(bk, [_lp("away")])
    assert len(send) == 1


def test_locked_stance_blocks_opposite_and_allows_same_side():
    clk = Clock()
    bk = stance.StanceBook(clock=clk)
    stance.gate(bk, [_lp("home")]); clk.tick()
    send, _ = stance.gate(bk, [_lp("home")])
    bk.commit(send[0], send[0]["stance_decision"])
    clk.tick()
    send, held = stance.gate(bk, [_lp("away", ev=0.06, p=0.62)])
    assert not send and held[0]["stance_decision"]["action"] in ("block", "hold")
    clk.tick()
    send, _ = stance.gate(bk, [_lp("home", market="h2h", line=None)])      # ML on the SAME team agrees
    assert len(send) == 1
    assert "Lakers" in bk.stance_text("nba|e1", "Lakers", "Celtics")


def test_total_and_team_stances_are_independent():
    clk = Clock()
    bk = stance.StanceBook(clock=clk)
    ps = [_lp("home"), _lp("over", market="totals", line=210.5)]
    stance.gate(bk, ps); clk.tick()
    send, _ = stance.gate(bk, ps)
    assert {stance.fam_of(p) for p in send} == {"team", "total"}


def test_reversal_needs_strength_swing_stability_and_is_labelled_once_per_window():
    clk = Clock()
    bk = stance.StanceBook(clock=clk)
    first = _lp("home", hs=30, as_=28)
    stance.gate(bk, [first]); clk.tick()
    send, _ = stance.gate(bk, [first])
    bk.commit(send[0], send[0]["stance_decision"])
    # weak opposite: blocked
    clk.tick()
    _, held = stance.gate(bk, [_lp("away", ev=0.05, p=0.60, hs=30, as_=28)])
    assert held[0]["stance_decision"]["action"] == "block"
    # strong opposite but the score has not moved: blocked
    for _ in range(4):
        clk.tick()
        send, held = stance.gate(bk, [_lp("away", ev=0.09, p=0.70, hs=30, as_=28)])
    assert not send and "score has not moved" in held[0]["stance_decision"]["reason"]
    # strong, the lead really swung (home +2 -> away +10), same read for 4 scans: reversal
    rev = None
    for _ in range(5):
        clk.tick()
        send, held = stance.gate(bk, [_lp("away", ev=0.09, p=0.70, hs=50, as_=60, period=2)])
        if send:
            rev = send[0]
            break
    assert rev is not None and rev["stance_decision"]["reversal"] is True and "lead moved" in rev["stance_decision"]["reason"]
    bk.commit(rev, rev["stance_decision"])
    assert bk.lock_of("nba|e1", "team")["dir"] == "away"
    # a second reversal in the same window is refused
    for _ in range(6):
        clk.tick()
        send, held = stance.gate(bk, [_lp("home", ev=0.09, p=0.70, hs=70, as_=60, period=2)])
    assert not send


def test_prematch_side_seeds_the_live_stance():
    clk = Clock()
    bk = stance.StanceBook(clock=clk)
    seeds = stance.seeds_from_picks([
        dict(market="spreads", selection="home", ev=0.06, status="new"),
        dict(market="totals", selection="under", ev=0.05, status="placed"),
        dict(market="totals_q1", selection="over", ev=0.09, status="new"),      # a Q1 total says nothing about the full game
        dict(market="h2h", selection="away", ev=0.04, status="skipped")])
    assert seeds == {"team": "home", "total": "under"}
    gate = lambda p: seeds
    stance.gate(bk, [_lp("away")], seeds_for=gate); clk.tick()
    send, held = stance.gate(bk, [_lp("away")], seeds_for=gate)
    assert not send                                    # opposite of the pre-match side: not an ordinary alert


def test_state_survives_a_restart_and_window_names():
    store = {}
    clk = Clock()
    bk = stance.StanceBook(load=lambda: store.get("s"), save=lambda s: store.__setitem__("s", s), clock=clk)
    stance.gate(bk, [_lp("home")]); bk.flush()
    bk2 = stance.StanceBook(load=lambda: store.get("s"), save=lambda s: None, clock=clk)
    assert bk2.reads("nba|e1", "team")["n"] == 1
    assert stance.window_of("nba", 2) == "w1" and stance.window_of("nba", 3) == "w2" and stance.window_of("ncaab", 2) == "w2"


def test_stance_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("STANCE_LOCK", "0")
    assert stance.enabled() is False


# ---------------------------------------------------------------- pre-match positions
class FakePin:
    def __init__(self, ev, rid="r1"):
        self.ev, self.rid = ev, rid

    def match(self, e, allow_started=False):
        return self.ev

    def refresh_id(self, lg):
        return self.rid


def _pos_pick(line=220.5, sel="over", odds=1.95, pid=7, market="totals"):
    c = datetime.utcnow() + timedelta(hours=5)
    return dict(mode="prematch", id=pid, league="nba", event_id="e1", home="Lakers", away="Celtics", commence=c.isoformat(),
                market=market, selection=sel, line=line, odds=odds, p=0.55, ev=0.07)


def _pin_with(line, o, u):
    return dict(markets={"totals": [dict(book="pinnacle", selection="over", line=line, odds=o),
                                    dict(book="pinnacle", selection="under", line=line, odds=u)]})


def test_position_holds_while_edge_stays_and_alerts_only_after_hold_time():
    clk = Clock()
    ps = positions.Positions(clock=clk)
    ps.register(_pos_pick())
    good = FakePin(_pin_with(220.5, 1.80, 2.05))          # fair over ~54.7% at 1.95 -> +6.7%: hold
    assert ps.check(good, []) == []
    bad = lambda rid: FakePin(_pin_with(220.5, 2.10, 1.75), rid)   # fair over ~45% -> negative edge
    out = []
    for i, rid in enumerate(("r2", "r3", "r4")):
        clk.tick(600)
        out = ps.check(bad(rid), [])
        if i < 2:
            assert out == []                              # not enough refreshes yet
    assert len(out) == 1 and out[0]["status"] == "exit"
    clk.tick(600)
    assert ps.check(bad("r5"), []) == []                  # announced once only


def test_a_one_point_line_move_against_the_bet_acts_at_once():
    clk = Clock()
    ps = positions.Positions(clock=clk)
    ps.register(_pos_pick(line=220.5, sel="over"))
    out = ps.check(FakePin(_pin_with(219.5, 1.9, 1.9)), [])   # Pinnacle total fell a full point: our Over line is now too high
    assert len(out) == 1 and out[0]["status"] == "exit" and "1.0 points against" in out[0]["detail"]


def test_line_moving_in_our_favour_is_fine():
    ps = positions.Positions(clock=Clock())
    ps.register(_pos_pick(line=220.5, sel="over"))
    assert ps.check(FakePin(_pin_with(222.5, 1.9, 1.9)), []) == []


def test_switch_when_the_other_side_qualifies_and_markets_are_independent():
    clk = Clock()
    ps = positions.Positions(clock=clk)
    ps.register(_pos_pick(pid=1, sel="over"))
    ps.register(_pos_pick(pid=2, sel="home", line=-3.5, market="spreads"))
    cand = dict(league="nba", event_id="e1", market="totals", selection="under", line=220.5, ev=0.06, p=0.58)
    pin = FakePin(dict(markets={"totals": _pin_with(220.5, 1.80, 2.05)["markets"]["totals"]}))
    out = []
    for rid in ("a", "b", "c", "d"):
        clk.tick(600)
        pin.rid = rid
        out += ps.check(pin, [cand])
    sw = [a for a in out if a["status"] == "switch"]
    assert len(sw) == 1 and sw[0]["pos"]["id"] == 1 and sw[0]["new"]["selection"] == "under"
    assert all(a["pos"]["id"] != 2 for a in out)          # the spread position was never touched


def test_new_pick_for_a_held_market_family_is_detected_and_other_families_are_not():
    ps = positions.Positions(clock=Clock())
    ps.register(_pos_pick())
    assert ps.conflicts_new(dict(league="nba", event_id="e1", market="totals", selection="under", line=222.5)) is not None
    assert ps.conflicts_new(dict(league="nba", event_id="e1", market="totals_q1", selection="over", line=55.5)) is None
    assert ps.conflicts_new(dict(league="nba", event_id="e2", market="totals", selection="over", line=1)) is None


def test_started_game_closes_and_logs_the_closing_line():
    clk = Clock()
    store = {}
    ps = positions.Positions(clock=clk, clv_save=lambda s: store.__setitem__("c", s))
    p = _pos_pick()
    p["commence"] = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
    ps.register(p)
    ps.check(FakePin(_pin_with(220.5, 1.8, 2.05)), [])
    assert ps.active() == [] and len(ps.clv_log) == 1 and json.loads(store["c"])[0]["id"] == 7


def test_positions_persist():
    store = {}
    ps = positions.Positions(save=lambda s: store.__setitem__("p", s), clock=Clock())
    ps.register(_pos_pick())
    ps2 = positions.Positions(load=lambda: store.get("p"), clock=Clock())
    assert [v["id"] for v in ps2.active()] == [7]


# ---------------------------------------------------------------- messages
def test_urgent_alert_is_loud_names_one_market_and_says_others_unchanged():
    from core.telegram_notify import format_position_alert
    v = dict(id=7, league="nba", home="Lakers", away="Celtics", market="totals", sel="over", line=220.5, odds=1.95)
    txt = format_position_alert(dict(pos=v, status="exit", detail="Pinnacle's line is now 219.5", new=None))
    assert "URGENT: PINNACLE MOVED" in txt and "Over 220.5" in txt and "Total" in txt and "other markets" in txt
    sw = format_position_alert(dict(pos=v, status="switch", detail="x", new=dict(home="Lakers", away="Celtics", market="totals",
                                                                                  selection="under", line=220.5)))
    assert "SWITCH SIDE" in sw and "Under 220.5" in sw
    assert "WATCH" in format_position_alert(dict(pos=v, status="watch", detail="x"))


def test_live_alert_shows_stance_reversal_and_tier():
    from core.telegram_notify import format_pick, tier
    p = dict(mode="live", id=3, league="nba", home="Lakers", away="Celtics", market="spreads", selection="away", line=-3.5,
             book="sportybet", odds=1.95, fair_odds=1.8, min_odds=1.85, ev=0.09, p=0.7, stake=5000, period=3,
             home_score=50, away_score=60, remaining_sec=300, stance="Celtics to win · Under",
             reversal="lead moved +2 to -10 since the stance was set")
    txt = format_pick(p)
    assert "REVERSAL" in txt and "Stance on this game" in txt and "Tier: <b>A</b>" in txt
    assert tier(dict(p=0.6)) == "B" and tier(dict(p=0.56, pin_real=False)) == "C"


def test_silent_summary(monkeypatch):
    from core import telegram_notify as tn
    sent = []
    monkeypatch.setattr(tn, "api", lambda method, **payload: sent.append(payload))
    tn.send("hello", chat_id="1", silent=True)
    tn.send("alert", chat_id="1")
    assert sent[0].get("disable_notification") is True and "disable_notification" not in sent[1]


def _outlook(period, halftime=False):
    return quarters.outlook("nba", period, 100.0, 40, 38, 3.0, 225.0, 4, 720, halftime=halftime) | dict(
        home="Lakers", away="Celtics", n=4, clock="9:00")


def test_quarter_phase_messages_follow_the_table():
    q1 = quarters.phase_message(_outlook(1))
    assert "QUARTER OUTLOOK" in q1 and "Rest of Q1" in q1 and "Q2 " in q1 and "1st half" in q1 and "Q3" not in q1
    assert "Forecast Only" in q1 and "<pre>" in q1
    q2 = quarters.phase_message(_outlook(2))
    assert "Rest of Q2" in q2 and "1st Half" in q2 and "Q4" not in q2 and "Q3" not in q2
    q3 = quarters.phase_message(_outlook(3), ht_total=60)
    assert "Earlier of Q3" in q3 and "Rest of Q3" in q3 and "Q4" in q3 and "2nd half" in q3
    q3b = quarters.phase_message(_outlook(3))        # half-time score unknown: no invented 'earlier' figure
    assert "Earlier of Q3" not in q3b and "Rest of 2nd Half" in q3b
    q4 = quarters.phase_message(_outlook(4), ht_total=60)
    assert "Rest of Q4" in q4 and "2nd half" in q4 and "Q3 " not in q4
    assert quarters.phase_message(_outlook(2, halftime=True)) is None     # nothing is sent at half-time
    assert quarters.phase_of(dict(n=2, period=1)) is None
    assert quarters.phase_of(_outlook(2, halftime=True)) == "HT"
