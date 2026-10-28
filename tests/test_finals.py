from datetime import datetime, timezone

from core import finals as F
from core import grader as G
from core.shadow import Shadow
from tests.test_grader import FakeRepo

T0 = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc).timestamp()      # tip-off


def _g(hs, a, period=4, rem=0, ht=False, lg="acb", home="Real Madrid", away="FC Barcelona"):
    return dict(league=lg, home=home, away=away, home_score=hs, away_score=a, period=period, remaining=rem,
                halftime=ht, break_after=None)


def _fin(now=[T0]):
    return F.Finals(load=lambda: None, save=lambda v: None, now=lambda: now[0]), now


def _pick(i, market, sel, line, lg="acb", status="new"):
    return dict(id=i, league=lg, home="Real Madrid", away="FC Barcelona",
                commence=datetime.fromtimestamp(T0, tz=timezone.utc), market=market, selection=sel, line=line,
                status=status, mode="prematch", book_odds=1.9, took_odds=None, fair_odds=1.75, stake=1000)


def _finished(hs, a, rem):
    f, now = _fin()
    now[0] = T0 + 3600
    f.observe([_g(hs, a, 4, rem)])
    assert f.tick() == 0                       # still inside the gap: not finished yet
    now[0] += 600
    assert f.tick() == 1
    return f, now


def test_game_is_final_only_after_it_left_the_feed_in_its_last_period():
    f, now = _fin()
    f.observe([_g(60, 50, period=2, rem=300)])
    now[0] += 3600
    assert f.tick() == 0 and f.done == {}      # left the feed in Q2: a feed gap, not a final
    f.observe([_g(88, 80, period=4, rem=600)])
    now[0] += 900
    assert f.tick() == 0                       # last seen with 10 minutes left: not a final either


def test_clear_results_are_graded_and_close_ones_are_not():
    f, _ = _finished(95, 80, 40)               # last seen 95-80 with 40 s left: cannot be caught
    d = f.lookup("acb", "Real Madrid", "FC Barcelona", T0)
    assert d and d["hs"] == 95
    assert F.grade(_pick(1, "h2h", "home", None), d)[0] == "won"
    assert F.grade(_pick(2, "totals", "over", 160.5), d)[0] == "won"       # 175 already, line far below
    assert F.grade(_pick(3, "totals", "under", 200.5), d)[0] == "won"      # 175 + 40 s cannot reach 200
    assert F.grade(_pick(4, "spreads", "home", -6.5), d)[0] == "won"       # 15 up; 40 s can swing at most 7
    assert F.grade(_pick(7, "spreads", "home", -10.5), d) is None          # 15 up is not safe against -10.5
    assert F.grade(_pick(5, "totals", "over", 176.5), d) is None           # could still go either way
    assert F.grade(_pick(6, "spreads", "home", -14.5), d) is None


def test_tie_or_near_tie_is_never_graded_because_of_overtime():
    f, _ = _finished(88, 88, 5)
    d = f.lookup("acb", "Real Madrid", "FC Barcelona", T0)
    assert F.grade(_pick(1, "h2h", "home", None), d) is None
    f2, _ = _finished(88, 87, 5)
    assert F.grade(_pick(1, "h2h", "home", None), f2.lookup("acb", "Real Madrid", "FC Barcelona", T0)) is None


def test_exact_final_with_no_time_left():
    f, _ = _finished(101, 99, 0)
    d = f.lookup("acb", "Real Madrid", "FC Barcelona", T0)
    assert F.grade(_pick(1, "totals", "over", 199.5), d) == ("won", (101, 99))      # 200 > 199.5, nothing left to play
    assert F.grade(_pick(2, "spreads", "away", -2.5), d) == ("won", (101, 99))       # spread lines are HOME lines: away +2.5 covers


def test_halftime_keeps_the_period_and_lookup_needs_the_right_game_and_day():
    f, now = _fin()
    f.observe([_g(50, 48, period=2, rem=0, ht=True)])
    assert list(f.last.values())[0]["rem"] == 0.0 and list(f.last.values())[0]["hs"] == 50
    f2, _ = _finished(95, 80, 40)
    assert f2.lookup("acb", "Real Madrid", "Valencia", T0) is None
    assert f2.lookup("acb", "Real Madrid", "FC Barcelona", T0 + 3 * 86400) is None


def test_grader_grades_real_and_paper_picks_of_leagues_without_a_score_api():
    f, now = _finished(95, 80, 40)
    sh = Shadow(load=lambda: None, save=lambda v: None, now=lambda: now[0])
    sh.record("k", dict(league="acb", home="Real Madrid", away="FC Barcelona",
                        commence=datetime.fromtimestamp(T0, tz=timezone.utc)),
              [dict(market="totals", selection="over", line=160.5, odds=1.9, p=0.6, ev=0.1)], [])
    repo, sent = FakeRepo([_pick(1, "h2h", "home", None, status="placed")]), []
    g = G.Grader(None, load=lambda: None, save=lambda v: None, now=lambda: now[0] + 4 * 3600, repo=repo,
                 send=sent.append, scores=lambda lg, d: [])
    g.finals, g.shadow = f, sh
    assert g.tick() == 2
    assert repo.settled == [(1, "won")] and len(sent) == 1 and "BET SETTLED (auto)" in sent[0]
    assert sh._d()["s:k"]["res"] == "won"
    assert g.tick() == 0


def test_closing_is_true_only_for_a_game_in_its_last_minutes():
    from core import finals as F
    clock = [1000.0]
    f = F.Finals(now=lambda: clock[0])
    g = dict(league="nba", home="A", away="B", home_score=90, away_score=80, period=4, halftime=False, remaining=900)
    f.observe([g])
    assert not f.closing()                       # 15 minutes left: normal 2-minute scan is enough
    clock[0] += 30
    f.observe([dict(g, remaining=300)])
    assert f.closing()                           # last 5 minutes: look every 30 s
    clock[0] += 900
    assert not f.closing()                       # nothing seen for 15 minutes: stop looking
