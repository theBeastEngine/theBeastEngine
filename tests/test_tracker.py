import datetime as dt
import json

import pytest

from core import tracker as T
from core.tracker import MatchTracker, plan_slots, book_lines, match_key

T0 = 1_792_300_000.0                      # an arbitrary "morning"
TIP = T0 + 4.5 * 3600


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ev(total=201.5, spread=-10.5):
    mk = {"totals": [dict(book="sportybet", selection="over", line=total, odds=1.85),
                     dict(book="sportybet", selection="under", line=total, odds=1.85)],
          "spreads": [dict(book="sportybet", selection="home", line=spread, odds=1.85),
                      dict(book="sportybet", selection="away", line=spread, odds=1.85)],
          "h2h": [dict(book="sportybet", selection="home", line=None, odds=1.20),
                  dict(book="sportybet", selection="away", line=None, odds=4.00)]}
    return dict(league="nbl", event_id="e1", home="Melbourne United", away="Illawarra Hawks", commence=_iso(TIP), markets=mk)


def _pin(total=201.0, spread=-10.0):
    mk = {"totals": [dict(book="pinnacle", selection="over", line=total, odds=1.9),
                     dict(book="pinnacle", selection="under", line=total, odds=1.9)],
          "spreads": [dict(book="pinnacle", selection="home", line=spread, odds=1.9),
                      dict(book="pinnacle", selection="away", line=spread, odds=1.9)]}
    return dict(league="nbl", event_id="e1", home="Melbourne United", away="Illawarra Hawks", commence=_iso(TIP), markets=mk)


CAND = dict(league="nbl", event_id="e1", home="Melbourne United", away="Illawarra Hawks", commence=_iso(TIP), market="totals",
            selection="under", side="under", line=201.5, odds=1.95, p=0.58, fair_odds=1.724, min_odds=1.79, book="sportybet",
            book_margin=0.08, ev=0.131)


class FakeRepo:
    def __init__(self):
        self.picks, self.n = {}, 0

    def add_pick(self, **kw):
        self.n += 1
        self.picks[self.n] = dict(kw, status="new")
        return self.n

    def mark_sent(self, i):
        pass

    def bankroll_current(self):
        return 100000.0

    def get_pick(self, i):
        return self.picks.get(i)


class FakePinref:
    def __init__(self):
        self.pin, self.cands = _pin(), []

    def match(self, ev, allow_started=False):
        return self.pin

    def candidates(self, ev, near=None):
        return [dict(c) for c in self.cands]

    def ensure_fresh(self, lg):
        return True

    forced_age = 1.0
    forced = 0

    def force_fresh(self, lg):
        self.forced += 1
        return self.forced_age


class FakeBeast:
    def __init__(self):
        self.pinref = FakePinref()
        self.injuries = type("I", (), {"note": lambda s, *a: None})()
        self.positions = type("P", (), {"register": lambda s, p: None})()

    def _hold_reason(self, c):
        return None

    def breaker_tripped(self):
        return False


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setenv("TRACKER", "1")
    monkeypatch.setenv("ALERT_TZ_OFFSET_HOURS", "1")
    sent, edits, clock = [], [], [T0]

    def send(text, markup, reply_to, silent):
        sent.append(dict(text=text, markup=markup, reply_to=reply_to, silent=silent))
        return 100 + len(sent)

    def edit(mid, text, markup):
        edits.append(dict(mid=mid, text=text, markup=markup))
        return True

    beast, repo = FakeBeast(), FakeRepo()
    store = {}
    tr = MatchTracker(beast, load=lambda: store.get("v"), save=lambda v: store.__setitem__("v", v), send=send, edit=edit,
                      now=lambda: clock[0], repo=repo)
    return tr, beast, repo, sent, edits, clock, store


def tick(tr, clock, t, ev=None):
    clock[0] = t
    tr.tick(events=[("nbl", ev or _ev())])


def test_book_lines_reads_main_lines():
    s = book_lines(_ev(), None)
    assert s["total"] == 201.5 and s["spread"] == -10.5 and s["ml"] == (1.20, 4.00)
    p = book_lines(_pin(), "pinnacle")
    assert p["total"] == 201.0 and p["spread"] == -10.0


def test_slots_three_checks_then_t60_and_final():
    s = plan_slots(T0, TIP)
    assert [x["kind"] for x in s] == ["check", "check", "check", "t60", "final", "t15", "last"]
    assert abs(s[3]["t"] - (TIP - 3600)) < 1 and abs(s[4]["t"] - (TIP - 1800)) < 1
    late = plan_slots(TIP - 3000, TIP)          # first seen 50 min before tip-off
    assert late[-3]["kind"] == "final" and late[-1]["kind"] == "last" and all(x["t"] >= TIP - 3000 for x in late)


def test_first_card_is_silent_watch_with_both_books(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    tick(tr, clock, T0)
    assert len(sent) == 1 and sent[0]["silent"] is True
    t = sent[0]["text"]
    assert "MATCH TRACKER" in t and "WATCH (no edge yet)" in t and "Pinnacle" in t and "SportyBet" in t
    assert "Total: +0.5" in t and "Injuries: none found" in t
    assert not repo.picks


def test_two_checks_in_a_row_make_play_now_and_ping(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    tick(tr, clock, T0)
    beast.pinref.cands = [CAND]
    beast.pinref.pin = _pin(total=199.5)
    tick(tr, clock, T0 + 1.2 * 3600)
    assert "Possible edge on Total: Under 201.5" in edits[-1]["text"] and not repo.picks          # one sighting is not enough
    assert "Pinnacle total fell 1.5, SportyBet did not follow." in edits[-1]["text"]
    tick(tr, clock, T0 + 2.4 * 3600)
    assert len(repo.picks) == 1 and "PLAY NOW" in edits[-1]["text"]
    assert "Under 201.5" in edits[-1]["text"] and "fair 1.72" in edits[-1]["text"]
    kb = edits[-1]["markup"]["inline_keyboard"]
    assert kb[0][0]["callback_data"].startswith("tog:1:") and kb[1][0]["callback_data"].startswith("tskip:")
    ping = sent[-1]
    assert ping["reply_to"] == 101 and "PLAY NOW" in ping["text"] and ping["silent"] is False
    # same message all the way: only ONE card was ever sent besides the ping
    assert len(sent) == 2


def test_t60_and_final_keep_the_same_card_and_ping_again(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.cands = [CAND]
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 1.2 * 3600)
    tick(tr, clock, T0 + 2.4 * 3600)
    pings = len(sent)
    beast.pinref.cands = [dict(CAND, odds=1.93, ev=0.119)]
    tick(tr, clock, TIP - 3600 + 5)
    assert "Still holding" in edits[-1]["text"] and "1.93" in edits[-1]["text"]
    beast.pinref.cands = [dict(CAND, odds=1.90, ev=0.10)]
    tick(tr, clock, TIP - 1800 + 5)
    assert "FINAL Check" in edits[-1]["text"] and "skip if it drops below 1.79" in edits[-1]["text"]
    assert len(sent) == pings + 2 and len(repo.picks) == 1               # no second pick for the same market


def test_edge_gone_is_said_and_no_bet_without_an_edge(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 1.2 * 3600)
    tick(tr, clock, T0 + 2.4 * 3600)
    assert not repo.picks and all("PLAY NOW" not in x["text"] for x in sent + edits)


def test_no_pinnacle_price_is_reported_but_never_a_bet(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.pin = None
    beast.pinref.cands = [CAND]
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 1.2 * 3600)
    tick(tr, clock, T0 + 2.4 * 3600)
    assert "NO PINNACLE PRICE" in sent[0]["text"] and not repo.picks


def test_skip_stops_the_card(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    tick(tr, clock, T0)
    key = match_key(_ev())
    assert tr.skip(key) is True
    n = len(edits)
    tick(tr, clock, T0 + 1.2 * 3600)
    assert len(edits) == n


def test_state_survives_a_restart(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    tick(tr, clock, T0)
    tr2 = MatchTracker(beast, load=lambda: store.get("v"), save=lambda v: store.__setitem__("v", v),
                       send=lambda *a: 999, edit=lambda *a: True, now=lambda: clock[0], repo=repo)
    clock[0] = T0 + 60
    tr2.tick(events=[("nbl", _ev())])            # nothing due yet: no second card
    assert json.loads(store["v"])["matches"] and len(sent) == 1


def test_long_card_collapses_old_blocks(rig, monkeypatch):
    tr, beast, repo, sent, edits, clock, store = rig
    monkeypatch.setenv("TRACK_MAX_CHARS", "900")
    beast.pinref.cands = [CAND]
    for dt_ in (0, 1.2, 2.4):
        tick(tr, clock, T0 + dt_ * 3600)
    tick(tr, clock, TIP - 3600 + 5)
    tick(tr, clock, TIP - 1800 + 5)
    last = edits[-1]["text"]
    assert len(last) <= 1500 and "FINAL Check" in last and last.count("Check 1") == 1


def test_skip_button_reaches_the_tracker(rig):
    from types import SimpleNamespace
    from services.telegram_bot import TelegramBot
    tr, beast, repo, sent, edits, clock, store = rig
    tick(tr, clock, T0)
    bot = TelegramBot.__new__(TelegramBot)
    bot.beast = SimpleNamespace(tracker=tr)
    toast, markup = bot.handle_callback("tskip:%s" % match_key(_ev()))
    assert "skipped" in toast.lower() and markup is None
    assert bot.handle_callback("tskip:nope")[0] == "Match not found"


def test_pinnacle_moving_away_blocks_agreement_and_is_said(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.cands = [dict(CAND, p=0.60)]
    tick(tr, clock, T0)
    beast.pinref.cands = [dict(CAND, p=0.575)]                       # Pinnacle's fair chance fell 2.5 points
    tick(tr, clock, T0 + 1.2 * 3600)
    assert not repo.picks and "Pinnacle moved away from Total: Under 201.5 (-2.5 pts)" in edits[-1]["text"]
    beast.pinref.cands = [dict(CAND, p=0.575)]                       # steady now: agrees with the previous check
    tick(tr, clock, T0 + 2.4 * 3600)
    assert len(repo.picks) == 1 and "PLAY NOW" in edits[-1]["text"]


def test_play_now_row_shows_direction(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.cands = [dict(CAND, p=0.58)]
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 1.2 * 3600)
    beast.pinref.cands = [dict(CAND, p=0.595)]
    tick(tr, clock, T0 + 2.4 * 3600)
    assert "Pinnacle moving toward us" in edits[-1]["text"] and "+1.5 pts" in edits[-1]["text"]


def test_zero_gap_shows_plain_zero(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.pin = _pin(total=201.5, spread=-10.5)
    tick(tr, clock, T0)
    assert "Total: 0.0 | Spread: 0.0" in sent[0]["text"] and "+0.0" not in sent[0]["text"]


def test_card_renders_after_restart_with_moneyline_lists(rig):
    """Saved state turns the moneyline tuple into a list; rendering must still work (was: must be real number, not list)."""
    tr, beast, repo, sent, edits, clock, store = rig
    tick(tr, clock, T0)
    st = json.loads(store["v"])
    for m in st["matches"].values():
        for c in m["checks"]:
            c["soft"]["ml"] = [1.5, 2.6]
            if c.get("pin"):
                c["pin"]["ml"] = [1.52, 2.5]
    store["v"] = json.dumps(st)
    tr2 = MatchTracker(beast, load=lambda: store.get("v"), save=lambda v: store.__setitem__("v", v),
                       send=lambda *a: 999, edit=lambda *a: True, now=lambda: clock[0], repo=repo)
    key = next(iter(tr2._state()["matches"]))
    text = tr2.render(tr2._state()["matches"][key])
    assert "Moneyline: 1.50 / 2.60" in text


def test_half_game_total_is_not_the_main_total():
    ev = dict(league="pba", home="A", away="B", markets={"totals": [
        dict(book="pinnacle", selection="over", line=102.5, odds=1.95), dict(book="pinnacle", selection="under", line=102.5, odds=1.95),
        dict(book="pinnacle", selection="over", line=207.5, odds=1.9), dict(book="pinnacle", selection="under", line=207.5, odds=2.0)]})
    assert book_lines(ev, "pinnacle")["total"] == 207.5


def test_t60_forces_a_fresh_pull_and_never_confirms_on_an_old_price(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.cands = [CAND]
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 1.2 * 3600)
    tick(tr, clock, T0 + 2.4 * 3600)
    assert len(repo.picks) == 1 and beast.pinref.forced == 0            # early checks use the cheap schedule
    beast.pinref.forced_age = 90.0
    tick(tr, clock, TIP - 3600 + 5)
    assert beast.pinref.forced == 1
    assert "Not confirming: the Pinnacle price is 90 min old." in edits[-1]["text"]
    beast.pinref.forced_age = 2.0
    tick(tr, clock, TIP - 1800 + 5)
    assert beast.pinref.forced == 2 and edits[-1]["text"].count("Not confirming") <= 1


def test_last_look_slot_five_minutes_before_tip(monkeypatch):
    s = plan_slots(1000.0, 1000.0 + 7200)
    assert s[-1]["kind"] == "last" and s[-1]["t"] == 1000.0 + 7200 - 300
    assert all(x["kind"] != "last" for x in plan_slots(1000.0, 1000.0 + 350))   # too close to tip-off to be worth a slot
    monkeypatch.setenv("LAST_LOOK", "0")
    assert all(x["kind"] != "last" for x in plan_slots(1000.0, 1000.0 + 7200))


def test_separate_verdict_message_at_each_checkpoint(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.cands = [CAND]
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 1.2 * 3600)
    tick(tr, clock, T0 + 2.4 * 3600)
    tick(tr, clock, TIP - 3600 + 5)
    v = sent[-1]
    assert "T-60 VERDICT" in v["text"] and "PLAY</b> Total: Under 201.5" in v["text"] and "play 1 of 1" in v["text"]
    assert v["silent"] is False and v["reply_to"] == 101                      # audible, a reply to the match card
    tick(tr, clock, TIP - 1800 + 5)
    assert "T-30 VERDICT" in sent[-1]["text"]
    beast.pinref.cands = []                                                    # the edge disappears
    tick(tr, clock, TIP - 900 + 5)
    assert "T-15 VERDICT" in sent[-1]["text"] and "SKIP</b> Total: Under 201.5" in sent[-1]["text"] and "skip this match" in sent[-1]["text"]
    tick(tr, clock, TIP - 300 + 5)
    assert "T-5 VERDICT" in sent[-1]["text"]
    assert len(repo.picks) == 1


def test_no_verdict_message_for_a_match_without_picks(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    tick(tr, clock, T0)
    n = len(sent)
    for t in (TIP - 3600 + 5, TIP - 1800 + 5, TIP - 900 + 5, TIP - 300 + 5):
        tick(tr, clock, t)
    assert len(sent) == n and all("VERDICT" not in x["text"] for x in sent)


def test_card_ends_with_summary_of_every_checkpoint(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.cands = [CAND]
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 1.2 * 3600)
    tick(tr, clock, T0 + 2.4 * 3600)
    tick(tr, clock, TIP - 3600 + 5)
    t = edits[-1]["text"]
    s = t[t.index("SUMMARY"):]
    assert "Pinnacle: Yes, from the start" in s and "T-60: <b>Play</b> (1 market)" in s
    assert "T-30: pending" in s and "T-15: pending" in s and "T-5: pending" in s
    assert "Markets to take" in s and "Total: Under 201.5 @ 1.95 (play at 1.79 or better)" in s
    beast.pinref.cands = []
    for x in (TIP - 1800 + 5, TIP - 900 + 5, TIP - 300 + 5):
        tick(tr, clock, x)
    s = edits[-1]["text"]
    s = s[s.index("SUMMARY"):]
    assert "T-30: Skip (edge gone)" in s and "T-5: Skip (edge gone)" in s and "Markets to take" not in s


def test_summary_for_match_with_no_pinnacle(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.pin = None
    tick(tr, clock, T0)
    s = sent[0]["text"]
    s = s[s.index("SUMMARY"):]
    assert "Pinnacle: No Pinnacle price so far" in s and "T-60: pending" in s


def test_checkpoint_verdicts_are_logged(rig):
    from core.cplog import CPLog
    tr, beast, repo, sent, edits, clock, store = rig
    tr.cplog = CPLog()
    beast.pinref.cands = [CAND]
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 1.2 * 3600)
    tick(tr, clock, T0 + 2.4 * 3600)
    tick(tr, clock, TIP - 3600 + 5)
    beast.pinref.cands = []
    tick(tr, clock, TIP - 1800 + 5)
    v = list(tr.cplog._d().values())[0]["v"]
    assert v["t60"][0] == "play" and v["final"][0] == "skip"


def _table_rows():
    r = lambda m, s, ln, o, f, ev: dict(market=m, selection=s, line=ln, odds=o, p=1 / f, fair_odds=f, ev=ev)
    return [r("h2h", "home", None, 1.20, 1.18, 0.017), r("h2h", "away", None, 4.00, 4.60, -0.13),
            r("spreads", "home", -10.5, 1.85, 1.90, -0.026), r("spreads", "away", -10.5, 1.85, 1.90, -0.026),
            r("totals", "over", 201.5, 1.85, 1.88, -0.016), r("totals", "under", 201.5, 1.85, 1.92, -0.036)]


def test_watch_card_uses_the_price_table_layout_like_tennis_and_football(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.table_rows = lambda ev: _table_rows()
    beast.pinref.pin = _pin(total=201.5, spread=-10.5)
    tick(tr, clock, T0)
    t = sent[0]["text"]
    assert "WATCH (no edge yet)" in t and "NO BET. Closest to a bet: Melbourne United to win (SportyBet 1.20, Pinnacle fair 1.18)" in t
    assert "Price columns: SportyBet | Pinnacle fair | edge" in t and "<b>MONEYLINE</b>" in t and "<b>SPREAD (closest line)</b>" in t
    assert "<b>TOTAL POINTS (closest line)</b>" in t and "Pinnacle's chances" in t and "Read: " in t
    assert "Melbourne" in t and "+1.7%" in t and "Total: 0.0 | Spread: 0.0" in t and "Injuries: none found" in t
    assert "Moneyline: 1.20" not in t                                 # the old Pinnacle / SportyBet lines are replaced by the table
    pre = [r for blk in t.split("<pre>")[1:] for r in blk.split("</pre>")[0].split("\n")]
    assert pre and all(len(r) <= 28 for r in pre)                      # every row fits one line on a phone
    assert not repo.picks


def test_card_falls_back_to_the_old_lines_when_the_price_table_cannot_be_built(rig):
    tr, beast, repo, sent, edits, clock, store = rig

    def boom(ev):
        raise RuntimeError("no table today")

    beast.pinref.table_rows = boom
    tick(tr, clock, T0)
    t = sent[0]["text"]
    assert "WATCH (no edge yet)" in t and "Total: +0.5" in t and "Price columns" not in t


def test_table_card_survives_a_restart(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    beast.pinref.table_rows = lambda ev: _table_rows()
    beast.pinref.pin = _pin(total=201.5, spread=-10.5)
    tick(tr, clock, T0)
    tr2 = MatchTracker(beast, load=lambda: store.get("v"), save=lambda v: store.__setitem__("v", v),
                       send=lambda *a: 999, edit=lambda *a: True, now=lambda: clock[0], repo=repo)
    key = next(iter(tr2._state()["matches"]))
    text = tr2.render(tr2._state()["matches"][key])
    assert "<b>MONEYLINE</b>" in text and "Pinnacle's chances" in text
