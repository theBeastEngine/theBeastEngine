"""Telegram redesign (15 Oct): one message per game, plain-English lines, toggle buttons, quarter outlook rows, hourly reports
(games / close calls / status), settled confirmation, daily wrap-up."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import services.telegram_bot as tb
from core import telegram_notify as tn, reports


def pk(i, market, sel, line, ev, p, odds, fair, **kw):
    d = dict(id=i, mode="prematch", league="nba", event_id="e1", home="Boston Celtics", away="New York Knicks", market=market,
             selection=sel, line=line, ev=ev, p=p, odds=odds, fair_odds=fair, min_odds=round(odds * 0.96, 2), stake=2000,
             book="sportybet", pin_real=True, commence="2026-10-14T23:30:00")
    d.update(kw)
    return d


PICKS = [pk(44, "h2h", "home", None, .041, .64, 1.58, 1.53, stake=1000), pk(41, "totals_h1", "over", 113.5, .072, .56, 1.93, 1.80),
         pk(43, "spreads_q1", "away", 1.5, .051, .53, 1.91, 1.82), pk(42, "totals", "under", 228.5, .06, .54, 1.95, 1.84),
         pk(45, "spreads", "home", -4.5, .044, .52, 1.96, 1.88)]


# ---- narration ----
def test_narration_total_spread_moneyline():
    assert "114 or more points" in tn.narrate(pk(1, "totals_h1", "over", 113.5, .07, .56, 1.9, 1.8))
    assert "228 points or fewer" in tn.narrate(pk(1, "totals", "under", 228.5, .07, .56, 1.9, 1.8))
    n = tn.narrate(pk(1, "spreads", "home", -4.5, .05, .55, 1.9, 1.8))          # home line -4.5: Celtics must win by 5+
    assert "Boston Celtics must win by 5 points or more" in n
    n = tn.narrate(pk(1, "spreads", "away", 4.5, .05, .55, 1.9, 1.8))          # home +4.5 -> Knicks -4.5
    assert "New York Knicks must win by 5 points or more" in n
    n = tn.narrate(pk(1, "spreads", "home", 2.5, .05, .55, 1.9, 1.8))          # Celtics +2.5: can lose by 2 or fewer
    assert "can lose by 2 points or fewer" in n
    n = tn.narrate(pk(1, "spreads", "home", 3.0, .05, .55, 1.9, 1.8))
    assert "refund" in n
    assert "win the game" in tn.narrate(pk(1, "h2h", "home", None, .05, .6, 1.6, 1.5))
    assert "1st quarter" in tn.narrate(pk(1, "spreads_q1", "home", -1.5, .05, .55, 1.9, 1.8))


# ---- one message per game ----
def test_game_alert_order_and_content():
    txt = tn.format_game_alert(PICKS, 100000)
    assert txt.count("\U0001F3AF") == 5 and "5 picks" in txt
    pos = [txt.index("[#%d]" % i) for i in (41, 42, 43, 45, 44)]       # High (EV order), Medium, Standard
    assert pos == sorted(pos)
    assert "HIGH" in txt and "MEDIUM" in txt and "STANDARD" in txt and "cap 15%" in txt
    assert "Pinnacle fair prices" in txt and "Tap a number" in txt
    assert "\U0001F4AC" in txt


def test_tier_sorts_inside_a_priority_block():
    a = pk(1, "totals", "over", 220.5, .05, .52, 1.9, 1.8)
    b = pk(2, "totals_h1", "over", 110.5, .045, .66, 1.9, 1.8)          # same priority, better tier, lower EV
    order = [p["id"] for p in sorted([a, b], key=tn.game_order)]
    assert order == [2, 1]


def test_live_and_sim_variants():
    live = [pk(7, "totals", "over", 158.5, .06, .55, 1.92, 1.81, mode="live", period=3, home_score=66, away_score=73,
               remaining_sec=340, stance="Bayern Munich to win")]
    t = tn.format_game_alert(live, 100000)
    assert "LIVE VALUE" in t and "MODEL ONLY" in t and "Valid for about" in t and "Stance on this game" in t and "66" in t
    sim = [pk(8, "totals", "under", 172.5, .055, .55, 1.94, 1.83, mode="sim")]
    assert "SIM VALUE" in tn.format_game_alert(sim, 100000) and "simulated" in tn.format_game_alert(sim, 100000)


def test_keyboard_toggle_row():
    kb = tn.game_keyboard(PICKS)["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["1", "2", "3", "4", "5"]
    assert all(len(b["callback_data"].encode()) <= 64 for b in kb)
    kb2 = tn.ids_keyboard([41, 42], {42})["inline_keyboard"][0]
    assert kb2[1]["text"].endswith("2") and kb2[1]["text"].startswith("✅") and kb2[0]["text"] == "1"


class Repo:
    def __init__(self):
        self.p = {i: dict(id=i, status="new", took_odds=None, book_odds=1.9, min_odds=1.85) for i in (41, 42)}

    def get_pick(self, i):
        return self.p.get(i)

    def set_status(self, i, s):
        if self.p[i]["status"] not in ("won", "lost", "void"):
            self.p[i]["status"] = s

    def set_took_odds(self, i, o):
        self.p[i]["took_odds"] = o


def test_toggle_places_then_unplaces(monkeypatch):
    r = Repo()
    monkeypatch.setattr(tb, "repo", r)
    bot = tb.TelegramBot(SimpleNamespace(paused=False))
    toast, kb = bot.handle_callback("tog:42:41,42")
    assert r.p[42]["status"] == "placed" and r.p[42]["took_odds"] == 1.9 and kb["inline_keyboard"][0][1]["text"].startswith("✅")
    toast, kb = bot.handle_callback("tog:42:41,42")
    assert r.p[42]["status"] == "new" and not kb["inline_keyboard"][0][1]["text"].startswith("✅")
    r.p[41]["status"] = "won"
    assert "settled" in bot.handle_callback("tog:41:41,42")[0].lower()


def test_groups_go_out_as_one_message_per_game(monkeypatch):
    from core import beast as bm
    sent = []
    monkeypatch.setattr(tn, "notify_game", lambda g: sent.append([p["id"] for p in g]) or True)
    b = bm.Beast.__new__(bm.Beast)
    marked = []
    monkeypatch.setattr(bm.repo, "mark_sent", lambda i: marked.append(i))
    picks = [dict(id=1, mode="prematch", league="nba", event_id="a"), dict(id=2, mode="prematch", league="nba", event_id="a"),
             dict(id=3, mode="prematch", league="nba", event_id="b")]
    b._send_groups(picks)
    assert sorted(sent) == [[1, 2], [3]] and sorted(marked) == [1, 2, 3]


# ---- quarter outlook ----
def test_quarter_rows_math():
    from core import quarters
    o = quarters.outlook("nba", 3, 60.0, 68, 65, 3.0, 216.0, 4, 720) | dict(home="A", away="B", n=4, clock="11:05")
    m = quarters.phase_message(o, ht_total=128)
    nums = {l.split(" .")[0].strip(): int(l.split(":")[1].split()[0]) for l in m.split("<pre>")[1].split("</pre>")[0].splitlines()}
    assert nums["Earlier of Q3"] == 5 and nums["2nd Half Forecast"] == nums["Earlier of Q3"] + nums["Rest of Q3"] + nums["Q4 Forecast"]


# ---- reports ----
NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)      # 09:00 WAT


def G(lg, h, a, c, priced):
    return dict(league=lg, home=h, away=a, commence=c, priced=priced)


GAMES = [G("nba", "Miami Heat", "Brooklyn Nets", datetime(2026, 10, 14, 23, 30), True),
         G("lnb", "Nancy", "Le Mans", datetime(2026, 10, 15, 18, 0), False)]


def test_games_report_first_then_same_as_then_additions():
    t, base = reports.games_report(GAMES, None, NOW, 2, 1, 29)
    assert "GAMES FOUND (2)" in t and "Pinnacle" in t and "No Pinnacle data" in t and "Miami Heat vs Brooklyn Nets" in t
    t2, base2 = reports.games_report(GAMES, base, NOW.replace(hour=9), 2, 1, 29)
    assert "same as 09:00" in t2 and "Miami" not in t2 and "ADDED" not in t2
    more = GAMES + [G("bbl", "Ulm", "Bayreuth", datetime(2026, 10, 15, 18, 30), True)]
    t3, base3 = reports.games_report(more, base2, NOW.replace(hour=10), 2, 1, 29)
    assert "ADDED since 09:00 (1)" in t3 and "Ulm vs Bayreuth" in t3 and "Miami" not in t3
    priced_later = [dict(GAMES[0]), dict(GAMES[1], priced=True)]
    t4, _ = reports.games_report(priced_later, base3, NOW.replace(hour=11))
    assert "NOW PRICED (1)" in t4 and "Nancy vs Le Mans" in t4
    t5, _ = reports.games_report(GAMES[:1], base3, NOW.replace(hour=12))
    assert "STARTED or no longer listed" in t5


def test_games_report_new_day_starts_from_scratch():
    _t, base = reports.games_report(GAMES, None, NOW, 1, 1, 1)
    t, _ = reports.games_report(GAMES, base, NOW.replace(day=16), 1, 1, 1)
    assert "GAMES FOUND" in t


def near(line=84.5, ev=0.006, p=0.52, sel="under", market="totals_h1"):
    return dict(league="acb", event_id="x", home="Joventut", away="Andorra", market=market, selection=sel, line=line, ev=ev, p=p, odds=1.9)


def test_close_calls_enter_after_two_scans_and_leave_after_three():
    cc = reports.CloseCalls()
    cc.update("prematch", [near()], now=1000.0)
    assert reports.close_calls_report(cc, "prematch", NOW) is None          # one sighting is not enough
    cc.update("prematch", [near()], now=2800.0)
    t = reports.close_calls_report(cc, "prematch", NOW)
    assert "CLOSE CALLS · PRE-MATCH" in t and "Under 84.5" in t and "held since" in t
    assert "EV" not in t and "%" not in t                                    # no numbers
    cc.update("prematch", [], now=4600.0)
    cc.update("prematch", [], now=6400.0)
    assert reports.close_calls_report(cc, "prematch", NOW) is not None       # 2 misses: still held
    cc.update("prematch", [], now=8200.0)
    assert reports.close_calls_report(cc, "prematch", NOW) is None           # 3 misses: gone


def test_close_calls_one_side_per_game_and_line_move():
    cc = reports.CloseCalls()
    for t in (1.0, 2.0):
        cc.update("prematch", [near(sel="under")], now=t)
    for t in (3.0, 4.0):
        cc.update("prematch", [near(sel="under"), near(sel="over")], now=t)   # the opposite side appears later
    txt = reports.close_calls_report(cc, "prematch", NOW)
    assert "Under 84.5" in txt and "Over 84.5" not in txt
    cc.update("prematch", [near(sel="under", line=86.5)], now=5.0)
    assert "Pinnacle line moved 84.5 → 86.5" in reports.close_calls_report(cc, "prematch", NOW)


def test_close_calls_modes_are_separate_and_marked():
    cc = reports.CloseCalls()
    for t in (1.0, 2.0):
        cc.update("sim", [near()], now=t)
    assert reports.close_calls_report(cc, "prematch", NOW) is None
    assert "SIM" in reports.close_calls_report(cc, "sim", NOW)


def test_status_report_has_no_alert_counts_and_dedupes_markets():
    snap = dict(n_live=3, live_games_max=3, live_priced_max=3, live_sent=1, sim=dict(games=28, priced=13, cands=72, sent=0),
                pre=dict(per_lg={"acb": [2, 0], "nba": [7, 7]}), n_pre=2, n_sim=1)
    ctx = dict(bankroll=100000, placed_today=0, open_bets=0, credits="51/300", papi="56/160", keyed={"acb", "nba"},
               last_alert_ago=7200.0, live_markets=dict(types=15, period_markets=[(47, "HT/FT", 1), (47, "HT/FT", 2)]))
    t = reports.status_report(snap, ctx, NOW)
    assert "ALERTS TODAY" not in t and "Last alert" in t and "2h ago" in t and t.count("HT/FT") == 1 and "No Pinnacle price for acb" in t


# ---- settled confirmation ----
def test_result_reply_shows_profit(monkeypatch):
    class R:
        def get_pick(self, i):
            return dict(id=41, status="placed", market="totals_h1", selection="over", line=113.5, home="A", away="B",
                        stake=2000, took_odds=1.93, book_odds=1.93, min_odds=1.86)

        def settle_pick(self, i, res):
            return 101860.0
    monkeypatch.setattr(tb, "repo", R())
    bot = tb.TelegramBot(SimpleNamespace(paused=False))
    out = bot.cmd_result("41 won")
    assert "BET SETTLED" in out and "WON" in out and "+₦1,860" in out and "101,860" in out


def test_link_notice_is_readable():
    from core import beast as bm
    t = bm.Beast._link_notice(["bsl: LINKED to Turkiye Super Lig tid=519 - 4 game(s) with Pinnacle prices; markets: h2h, spreads, totals"])
    assert "NEW PINNACLE SOURCE LINKED" in t and "Turkey BSL" in t and "tid 519" in t and "4 games priced" in t


def test_wrapup_text(monkeypatch):
    from core import beast as bm
    b = bm.Beast.__new__(bm.Beast)
    done = [dict(status="won", stake=2000, took_odds=1.93, book_odds=1.93, min_odds=1.8),
            dict(status="lost", stake=1000, took_odds=None, book_odds=1.9, min_odds=1.8)]
    made = [dict(status="won"), dict(status="lost"), dict(status="placed"), dict(status="new"), dict(status="skipped")]
    monkeypatch.setattr(bm.repo, "created_between", lambda a, b_: made)
    monkeypatch.setattr(bm.repo, "settled_between", lambda a, b_: done)
    monkeypatch.setattr(bm.repo, "placed_bets", lambda n: [dict(status="placed")])
    monkeypatch.setattr(bm.repo, "bankroll_current", lambda: 100860.0)
    t = b.wrapup_text(datetime(2026, 10, 15, 22, 30, tzinfo=timezone.utc))
    assert "DAILY WRAP-UP" in t and "Placed <b>3</b>" in t and "Won <b>1</b>" in t and "Lost <b>1</b>" in t and "Open <b>1</b>" in t
    assert "+₦860" in t and "₦100,860" in t and "skipped: <b>2</b>" in t


def test_build_reports_end_to_end(monkeypatch):
    from core import beast as bm
    from core.digest import ScanStats
    b = bm.Beast.__new__(bm.Beast)
    b.stats, b.closecalls = ScanStats(), reports.CloseCalls()
    kv = {}
    monkeypatch.setattr(bm.repo, "kv_get", lambda k: kv.get(k))
    monkeypatch.setattr(bm.repo, "kv_set", lambda k, v: kv.__setitem__(k, v))
    monkeypatch.setattr(bm, "gather_ctx", lambda self: dict(bankroll=100000, placed_today=0, open_bets=0, credits="1/300",
                                                              papi="1/160", keyed=set()))
    b.stats.record_prematch(2, 1, {"nba": [1, 1], "lnb": [1, 0]}, 0, 0, [], [], games=GAMES)
    b.stats.record_sim(5, 2, 3, 0)
    for t in (1.0, 2.0):
        b.closecalls.update("prematch", [near()], now=t)
    out = b.build_reports(reset=True, now=NOW)
    assert len(out) == 3 and "GAMES FOUND (2)" in out[0] and "CLOSE CALLS" in out[1] and "ENGINE STATUS" in out[2]
    assert "report_games_baseline" in kv
    b.stats.record_prematch(2, 1, {"nba": [1, 1]}, 0, 0, [], [], games=GAMES)
    out2 = b.build_reports(reset=True, now=NOW.replace(hour=9))
    assert "same as 09:00" in out2[0]


def test_situation_room_lines():
    from core import quarters
    base = dict(home="Brisbane", away="Phoenix", n=4, clock="2:17")
    o = quarters.outlook("nbl", 2, 463.0, 47, 45, -8.0, 201.0, 4, 600) | base
    m = quarters.phase_message(o)
    assert m.splitlines()[2:4] == ["Brisbane: 47", "Phoenix: 45"]
    assert "<b>Market Situation Room</b>" in m and "1. Phoenix favoured" in m and "3. Spread Phoenix -" in m
    assert "<b>Forecast Only.</b>\nNo bookmaker price is checked, so this is not a bet." in m
    hot = quarters.outlook("nbl", 2, 463.0, 60, 55, 0.0, 201.0, 4, 600) | base
    assert "2. Over 100.5 (1st half, pre-game pace)" in quarters.phase_message(hot)
    cold = quarters.outlook("nbl", 2, 463.0, 38, 36, 0.0, 201.0, 4, 600) | base
    assert "2. Under 100.5" in quarters.phase_message(cold)
    even = quarters.outlook("nbl", 2, 463.0, 46, 45, 0.0, 201.0, 4, 600) | base
    assert "1. No clear favourite" in quarters.phase_message(even) and "3. Spread: pick'em" in quarters.phase_message(even)


def _wire_cards(b, store, sent):
    from core import livecards, qgrade
    b.cards = livecards.LiveCards(load=lambda: store.get("live_cards"), save=lambda v: store.__setitem__("live_cards", v),
                                  send=lambda t, silent: (sent.append(t), len(sent))[1],
                                  edit=lambda m, t: (sent.__setitem__(m - 1, t), True)[1])
    b.qgrade = qgrade.QGrade()


def test_quarter_state_survives_a_restart(monkeypatch):
    """Half-time scores and sent phases are stored, so a deploy mid-game neither loses 'Earlier of Q3' nor re-sends a phase."""
    from core import beast as bm, quarters, telegram_notify as tn
    store, sent = {}, []
    monkeypatch.setattr(bm.repo, "kv_get", lambda k: store.get(k))
    monkeypatch.setattr(bm.repo, "kv_set", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(tn, "send", lambda text, **k: sent.append(text))

    def mk(outl):
        b = bm.Beast.__new__(bm.Beast)
        b._ht_scores, b._ht_seen, b._q3_scores, b._q1_scores, b._quarter_sent, b._quarter_loaded = {}, {}, {}, {}, {}, False
        b.live_scanner = SimpleNamespace(outlooks=[outl])
        _wire_cards(b, store, sent)
        return b
    base = dict(home="A", away="B", n=4, clock="11:00")
    ht = quarters.outlook("nba", 0, 0.0, 56, 55, 0.0, 200.0, 4, 720, halftime=True) | base
    q3 = quarters.outlook("nba", 3, 60.0, 70, 65, 0.0, 200.0, 4, 720) | base
    mk(ht)._quarter_phases()                      # half-time seen: stored
    assert '"ht"' in store["quarter_state"] and sent == []
    b2 = mk(q3)                                    # restart: fresh object, nothing in memory
    b2._quarter_phases()
    assert len(sent) == 1 and "Earlier of Q3" in sent[0] and "2nd Half Forecast" in sent[0]
    b3 = mk(q3)                                    # another restart: the same phase is not sent twice
    b3._quarter_phases()
    assert len(sent) == 1


def test_break_numbers_and_q4_rows(monkeypatch):
    """Only the 2nd break is half-time; the 3rd break score starts Q4. Q4 = Earlier of Q4 / Rest of Q4 / 2nd half (finished Q3 + both)."""
    from core import beast as bm, quarters, telegram_notify as tn
    from core.bookmakers.sportybet import _break_after
    assert _break_after("2nd break", 0, 4) == 2 and _break_after("Halftime", 0, 4) == 2
    assert _break_after("3rd break", 3, 4) == 3 and _break_after("1st break", 1, 4) == 1 and _break_after("Break", 0, 4) is None
    base = dict(home="A", away="B", n=4, clock="5:00")
    ht = quarters.outlook("nba", 0, 0.0, 56, 55, 0.0, 200.0, 4, 720, halftime=True, break_after=2) | base
    b3 = quarters.outlook("nba", 0, 0.0, 80, 71, 0.0, 200.0, 4, 720, halftime=True, break_after=3) | base
    b1 = quarters.outlook("nba", 0, 0.0, 30, 28, 0.0, 200.0, 4, 720, halftime=True, break_after=1) | base
    assert quarters.phase_of(ht) == "HT" and quarters.phase_of(b3) == "B3" and quarters.phase_of(b1) == "B1"
    assert b3["quarters"][0]["label"].startswith("Q4")                       # the next period after the 3rd break is Q4
    q4 = quarters.outlook("nba", 4, 420.0, 90, 80, 0.0, 200.0, 4, 720) | base
    m = quarters.phase_message(q4, ht_total=111, q3_total=151)
    rows = {l.split(" .")[0].strip(): int(l.split(":")[1].split()[0]) for l in m.split("<pre>")[1].split("</pre>")[0].splitlines()}
    assert list(rows) == ["Q3 Break", "Earlier Q4", "Rest of Q4", "2nd Half"]
    assert rows["Q3 Break"] == 40 and rows["Earlier Q4"] == 170 - 151 and rows["2nd Half"] == 40 + rows["Earlier Q4"] + rows["Rest of Q4"]
    store, sent = {}, []
    monkeypatch.setenv("QUARTER_PHASES", "1,2,3,4")   # the default is Q1 and Q3 only; this test exercises Q4
    monkeypatch.setattr(bm.repo, "kv_get", lambda k: store.get(k))
    monkeypatch.setattr(bm.repo, "kv_set", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(tn, "send", lambda text, **k: sent.append(text))

    def run(outl):
        b = bm.Beast.__new__(bm.Beast)
        b._ht_scores, b._ht_seen, b._q3_scores, b._q1_scores, b._quarter_sent, b._quarter_loaded = {}, {}, {}, {}, {}, False
        b.live_scanner = SimpleNamespace(outlooks=[outl])
        _wire_cards(b, store, sent)
        b._quarter_phases()
    for o in (b1, ht, b3):
        run(o)                                     # each break is recorded separately, nothing is sent
    assert sent == [] and '"q3"' in store["quarter_state"]
    run(q4)                                        # a restart later: both scores come back from the database
    assert len(sent) == 1 and "Earlier Q4" in sent[0] and "Q3 Break" in sent[0]


def test_q1_q2_rows_and_partial_pinnacle():
    from core import quarters
    from core.pinref import PinRef
    base = dict(home="A", away="B", n=4, clock="5:00")
    q1 = quarters.phase_message(quarters.outlook("kbl", 1, 240.0, 8, 7, 0.0, 160.0, 4, 600) | base)
    labs = [l.split(" .")[0].strip() for l in q1.split("<pre>")[1].split("</pre>")[0].splitlines()]
    assert labs == ["Earlier of Q1", "Rest of Q1", "Q2 Forecast", "1st half Forecast"]
    q2 = quarters.phase_message(quarters.outlook("kbl", 2, 240.0, 62, 50, 0.0, 160.0, 4, 600) | base, q1_total=45)
    rows = {l.split(" .")[0].strip(): int(l.split(":")[1].split()[0]) for l in q2.split("<pre>")[1].split("</pre>")[0].splitlines()}
    assert list(rows) == ["Q1 Break", "Earlier Q2", "Rest of Q2", "1st Half"] and rows["Q1 Break"] == 45 and rows["Earlier Q2"] == 67
    assert rows["1st Half"] == 45 + 67 + rows["Rest of Q2"]
    part = quarters.phase_message(quarters.outlook("kbl", 2, 240.0, 62, 50, 3.0, 160.0, 4, 600, source="pinnacle_spread") | base)
    assert "Pinnacle spread used" in part and "League averages used" not in part


def test_pinref_parts_say_what_is_missing():
    from core.pinref import PinRef
    pr = PinRef.__new__(PinRef)
    mk = {"spreads": [dict(book="pinnacle", selection="home", line=-3.5, odds=1.9), dict(book="pinnacle", selection="away", line=-3.5, odds=1.9)]}
    pr.match = lambda ev, allow_started=False: dict(markets=mk)
    mu, T, why = pr.pregame_parts(dict(league="kbl"))
    assert mu == 3.5 and T is None and why == "no Pinnacle total" and pr.pregame_params(dict(league="kbl")) is None
    pr.match = lambda ev, allow_started=False: None
    assert pr.pregame_parts(dict(league="kbl"))[2].startswith("no cached Pinnacle game")
