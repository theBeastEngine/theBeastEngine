import time

from core import agenda, livecards, qcard
from core.reports import CloseCalls, close_calls_card, status_warnings
from datetime import datetime, timezone


def mk(now=1000.0):
    sent, edits = [], []
    clock = [now]

    def send(text, silent):
        sent.append((text, silent))
        return 10 + len(sent)

    def edit(mid, text):
        edits.append((mid, text))
        return True
    store = {}
    c = livecards.LiveCards(load=lambda: store.get("v"), save=lambda v: store.__setitem__("v", v), send=send, edit=edit,
                            now=lambda: clock[0])
    return c, sent, edits, clock, store


def test_card_sent_once_then_edited_then_new_day():
    c, sent, edits, clock, store = mk()
    assert c.put("k", "A", "d1") == "sent"
    assert c.put("k", "A", "d1") == ""            # same text: nothing
    assert c.put("k", "B", "d1") == "edited"
    assert len(sent) == 1 and edits == [(11, "B")]
    assert c.put("k", "B", "d2") == "sent"       # a new local day, a new card
    assert len(sent) == 2 and sent[0][1] is True  # silent


def test_card_survives_restart_and_edit_failure_resends():
    c, sent, edits, clock, store = mk()
    c.put("k", "A", "d1")
    c2 = livecards.LiveCards(load=lambda: store["v"], save=lambda v: None, send=lambda t, s: 99, edit=lambda m, t: False)
    assert c2.get("k")["msg_id"] == 11
    assert c2.put("k", "B", "d1") == "sent"       # edit refused -> fresh card
    assert c2.get("k")["msg_id"] == 99


def test_card_data_kept():
    c, *_ = mk()
    c.put("k", "A", "d1", data={"x": 1})
    c.put("k", "B", "d1")
    assert c.get("k")["data"] == {"x": 1}


def _ev(h, a, t):
    return dict(home=h, away=a, commence=datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_agenda_collect_adds_never_removes_and_marks_started():
    now = time.time()
    g = agenda.collect(now, [("nbl", _ev("A", "B", now + 3600))], [])
    assert len(g) == 1
    g = agenda.collect(now + 7200, [("nbl", _ev("A", "B", now + 3600)), ("nbl", _ev("C", "D", now + 9000))], g)
    assert len(g) == 2                           # A-B started but is kept; C-D added
    txt = "\n".join(agenda.render(now + 7200, g, lambda x: x["home"] == "C"))
    assert "▶" in txt and "✅" in txt and "A vs B" in txt


def test_close_calls_one_card_all_modes():
    t = close_calls_card(CloseCalls(), datetime.now(timezone.utc))
    assert t.count("CLOSE CALLS") == 1 and "PRE-MATCH" in t and "LIVE" in t and "SIM" in t


def test_status_warnings_from_coverage():
    w = status_warnings({"pre": {"per_lg": {"nbl": (5, 0)}}}, {"keyed": ["nbl"]})
    assert w and "nbl" in w[0]


LINES = {"totals_q1": [dict(selection="over", line=50.5, odds=1.85), dict(selection="under", line=50.5, odds=1.85)],
         "totals_h1": [dict(selection="over", line=100.5, odds=1.9), dict(selection="under", line=100.5, odds=1.8)]}


def test_takes_only_when_real_line_and_clear_gap():
    t = qcard.takes({"Q1": 55.0, "1H": 101.0, "Q2": 50.0}, LINES)
    assert [(x["label"], x["side"]) for x in t] == [("Q1", "OVER")]      # 1H too close; Q2 has no line
    t = qcard.takes({"Q1": 46.0}, LINES)
    assert t[0]["side"] == "UNDER" and t[0]["odds"] == 1.85
    assert qcard.takes({"Q1": 70.0}, {}) == []


def test_take_lines_say_why_none():
    assert "no quarter" in qcard.take_lines({"Q1": 70.0}, {})[0]
    assert "too close" in qcard.take_lines({"Q1": 51.0}, LINES)[0]
    assert "TAKE OVER 50.5" in qcard.take_lines({"Q1": 56.0}, LINES)[0]


def test_qcard_render_has_result_check_and_unproven():
    data = dict(sec={"1": "Q1 SECTION"}, take={"1": ["TAKE LINE"]})
    txt = qcard.render("nba", "A", "B", data, [(1, {"Q1": 52.0}, {"Q1": 47})], graded=3)
    assert "Q1 SECTION" in txt and "TAKE LINE" in txt and "actual 47" in txt and "3 of 30" in txt


def test_send_digest_two_cards_and_one_warning(monkeypatch):
    from core import beast as bm, telegram_notify as tn
    from core.digest import ScanStats
    store, loud = {}, []
    b = bm.Beast.__new__(bm.Beast)
    b.stats, b.closecalls, b.notify = ScanStats(), CloseCalls(), object()
    cards, sent, edits, clock, cstore = mk()
    b.cards = cards
    monkeypatch.setattr(bm.repo, "kv_get", lambda k: store.get(k))
    monkeypatch.setattr(bm.repo, "kv_set", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(bm, "gather_ctx", lambda self: dict(bankroll=100000, placed_today=0, open_bets=0, credits="1/300",
                                                              papi="1/160", keyed={"nbl"}, warnings=[]))
    monkeypatch.setattr(tn, "send", lambda text, **k: loud.append(text) or True)
    b.stats.record_prematch(2, 1, {"nbl": [5, 0]}, 0, 0, [], [], games=[])
    b.send_digest()
    assert len(sent) == 2 and "ENGINE STATUS" in sent[0][0] and "CLOSE CALLS" in sent[1][0]
    assert len(loud) == 1 and "ENGINE WARNING" in loud[0]
    b.stats.record_prematch(2, 1, {"nbl": [4, 0]}, 0, 0, [], [], games=[])
    b.send_digest()
    assert len(sent) == 2 and len(edits) >= 1       # same two cards edited, not re-sent
    assert len(loud) == 1                            # same warning (only the count changed): not repeated
