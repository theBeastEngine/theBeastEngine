import json
from core import qgrade as Q
from core.blocks import Blocks


class Fin:
    def __init__(self, d):
        self.d = d

    def lookup(self, lg, h, a, c):
        return self.d


def test_forecasts_for_q1_and_q3_messages():
    it = dict(home_score=10, away_score=8, quarters=[dict(label="Q1 (rest)", exp_pts=30.0), dict(label="Q2", exp_pts=46.0),
                                                      dict(label="Q3", exp_pts=45.0), dict(label="Q4", exp_pts=47.0)])
    assert Q.forecasts(it, 1) == {"Q1": 48.0, "Q2": 46.0, "1H": 94.0}
    it3 = dict(home_score=100, away_score=90, quarters=[dict(label="Q3", exp_pts=30.0), dict(label="Q4", exp_pts=44.0)])
    assert Q.forecasts(it3, 3, ht_total=180) == {"Q4": 44.0, "Q3": 40.0, "2H": 84.0}
    assert Q.forecasts(it3, 3) == {"Q4": 44.0}


def test_quarters_are_graded_from_the_break_totals_and_a_clean_final():
    box = {}
    q = Q.QGrade(load=lambda: box.get("v"), save=lambda v: box.__setitem__("v", v), now=lambda: 1000.0,
                 finals=Fin(dict(rem=0, period=4, n=4, hs=110, as_=100)))
    gk = ("nba", "A", "B")
    assert q.record(gk, 1, {"Q1": 50.0, "Q2": 46.0, "1H": 96.0}) and not q.record(gk, 1, {"Q1": 1})
    q.record(gk, 3, {"Q4": 44.0, "Q3": 40.0, "2H": 84.0})
    q.observe(q1={gk: 52}, ht={gk: 100}, q3={gk: 140})
    assert q.grade() >= 5
    rows = {r[0]: r for r in q.rows()}
    assert rows["Q1"][2] == 2.0 and rows["Q2"][2] == 2.0 and rows["Q3"][2] == 0.0          # actual 52, 48, 40
    assert rows["Q4"][2] == 26.0 and rows["2H"][2] == 26.0                                  # final 210: Q4 70, 2H 110
    assert json.loads(box["v"])                                                             # saved


def test_q4_not_graded_when_the_final_is_not_exact():
    q = Q.QGrade(now=lambda: 1000.0, finals=Fin(dict(rem=40, period=4, n=4, hs=110, as_=100)))
    gk = ("nba", "A", "B")
    q.record(gk, 3, {"Q4": 44.0})
    q.observe(ht={gk: 100}, q3={gk: 140})
    q.grade()
    assert q.rows() == []


def test_ticket_is_graded_from_its_legs():
    st = {}
    b = Blocks(load=lambda: json.dumps(st) if st else None, save=lambda v: st.update(json.loads(v)), now=lambda: 5000.0)
    s = b._st()
    s["tickets"] = {"k1": dict(day="Tue 20 Oct", t=1, legs=[dict(id=1, odds=1.5, p=.66), dict(id=2, odds=1.6, p=.64)], odds=2.4, p=.42, res=None),
                    "k2": dict(day="Tue 20 Oct", t=1, legs=[dict(id=3, odds=1.5, p=.66), dict(id=4, odds=1.6, p=.64)], odds=2.4, p=.42, res=None),
                    "k3": dict(day="Tue 20 Oct", t=1, legs=[dict(id=5, odds=1.5, p=.66), dict(id=6, odds=1.6, p=.64)], odds=2.4, p=.42, res=None)}
    res = {1: "won", 2: "won", 3: "won", 4: "lost", 5: "won", 6: None}
    assert b.settle(lambda pid: res.get(pid)) == 2
    assert s["tickets"]["k1"]["res"] == "won" and s["tickets"]["k2"]["res"] == "lost" and s["tickets"]["k3"]["res"] is None
    n, w, exp, ret = b.ticket_rows()
    assert (n, w) == (2, 1) and round(ret, 1) == round(100 * ((1.4) - 1.0) / 2, 1)
