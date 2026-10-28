from datetime import datetime, timezone

from core import grader as G
from core.shadow import Shadow
from tests.test_tracker import rig, tick, CAND, T0, TIP, _ev   # noqa: F401  (rig is a fixture)
from tests.test_grader import FakeRepo, _score, NOW


def _store():
    box = {}
    return Shadow(load=lambda: box.get("v"), save=lambda v: box.__setitem__("v", v), now=lambda: NOW), box


EV = dict(league="nbl", home="Melbourne United", away="Illawarra Hawks",
          commence=datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc))
EDGE = dict(market="totals", selection="over", line=180.5, odds=1.95, p=0.57, ev=0.11)
NEAR = [dict(market="spreads", selection="home", line=-10.5, odds=1.80, p=0.54, ev=-0.02),
        dict(market="totals", selection="under", line=180.5, odds=1.85, p=0.55, ev=0.0175)]


def test_choose_prefers_a_cleared_edge_else_the_best_close_one():
    assert Shadow.choose([EDGE], NEAR)[0] == "edge"
    kind, c = Shadow.choose([], NEAR)
    assert kind == "close" and c["selection"] == "under"
    assert Shadow.choose([], []) is None
    q1 = dict(EDGE, market="totals_q1")
    assert Shadow.choose([q1], []) is None            # quarter markets cannot be graded: never recorded


def test_record_once_per_match_and_survives_reload():
    sh, box = _store()
    assert sh.record("k1", EV, [], NEAR) is True
    assert sh.record("k1", EV, [EDGE], NEAR) is False
    sh2 = Shadow(load=lambda: box.get("v"), save=lambda v: None)
    assert sh2.counts()["total"] == 1 and sh2.pending()[0]["market"] == "totals"


def test_shadow_off_records_nothing(monkeypatch):
    monkeypatch.setenv("SHADOW", "0")
    sh, _ = _store()
    assert sh.record("k1", EV, [EDGE], NEAR) is False


def test_grader_grades_paper_picks_without_touching_real_bets_or_messages():
    sh, _ = _store()
    sh.record("k1", EV, [EDGE], [])                    # over 180.5, final 95-88 = 183 -> won
    repo, sent = FakeRepo([]), []
    g = G.Grader(None, load=lambda: None, save=lambda v: None, now=lambda: NOW, repo=repo, send=sent.append,
                 scores=lambda lg, days: _score())
    g.shadow = sh
    assert g.tick() == 1
    assert sh._d()["s:k1"]["res"] == "won" and sh._d()["s:k1"]["score"] == "95-88"
    assert repo.settled == [] and sent == [] and g.grades == {}


def test_rows_split_edge_and_close_and_skip_void():
    sh, _ = _store()
    sh.record("a", EV, [EDGE], [])
    sh.record("b", dict(EV, home="X", away="Y"), [], NEAR)
    sh._d()["s:a"]["res"] = "won"
    sh._d()["s:b"]["res"] = "lost"
    rows = {r[0]: r for r in sh.rows()}
    assert rows["Paper: edge seen, not played"][1:3] == (1, 1)
    assert rows["Paper: closest, no edge"][1:3] == (1, 0)
    sh._d()["s:b"]["res"] = "void"
    assert "Paper: closest, no edge" not in {r[0] for r in sh.rows()}


def test_tracker_card_shows_why_no_pick_and_records_a_paper_pick_at_final(rig):
    tr, beast, repo, sent, edits, clock, store = rig
    sh, _ = _store()
    tr.shadow = sh
    close = dict(market="totals", selection="over", line=201.5, odds=1.88, p=0.53, ev=0.0164,
                 home="Melbourne United", away="Illawarra Hawks")
    beast.pinref.candidates = lambda ev, near=None: (near.append(dict(close)) if near is not None else None) or []
    tick(tr, clock, T0)
    t = sent[0]["text"]
    assert "Why no pick" in t and "Total: Over 201.5 | 53% | edge +1.6%, needs +4%" in t
    assert sh.counts()["total"] == 0                   # only the FINAL check records
    tick(tr, clock, TIP - 1800)
    assert sh.counts()["total"] == 1 and list(sh._d().values())[0]["kind"] == "close"


def test_shadow_command_and_menu():
    from services import telegram_bot as tb
    assert "shadow" in [c for c, _ in tb.COMMANDS] and hasattr(tb.TelegramBot, "cmd_shadow")


def test_breakdown_groups_by_market_league_and_win_chance():
    from core.shadow import Shadow
    sh = Shadow(load=lambda: None, save=lambda b: None)
    d = sh._d()
    rows = [("h2h", "nba", 0.66, 1.6, "won"), ("h2h", "nba", 0.66, 1.6, "lost"), ("totals", "pba", 0.56, 1.95, "lost")]
    for i, (m, lg, p, o, r) in enumerate(rows):
        d["s:%d" % i] = dict(id="s:%d" % i, kind="edge", market=m, league=lg, odds=o, p=p, fair_odds=1 / p, res=r, t=i)
    b = sh.breakdown()
    assert b["market"][0][:3] == ("Moneyline", 2, 1) and b["league"][-1][0] == "PBA"
    assert [x[0] for x in b["win chance"]] == ["62-67%", "under 58%"]


def test_paper_picks_skip_tiny_odds():
    from core.shadow import Shadow
    near = [dict(market="h2h", selection="home", odds=1.05, p=0.93, ev=-0.02), dict(market="h2h", selection="away", odds=1.9, p=0.5, ev=-0.05)]
    assert Shadow.choose([], near)[1]["odds"] == 1.9
    assert Shadow.choose([], near[:1]) is None
