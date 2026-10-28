"""/addleague, /probe, /removeleague: parsing, live merge into the config, persistence, restart reload."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from config.leagues import LEAGUES, LEAGUE_KEYS
from core import leagues_dyn
from core.bookmakers import _base


def _clean():
    for c in [c for c in list(LEAGUES) if c not in leagues_dyn.BUILTIN]:
        leagues_dyn.remove_live(c)


def cleans(fn):
    """Added leagues live in module-level dicts: always remove them, even when a test fails."""
    import functools

    @functools.wraps(fn)
    def run(*a, **k):
        _clean()
        try:
            return fn(*a, **k)
        finally:
            _clean()
    return run


def _ev(total, spread):
    mk = {"totals": [dict(book="pinnacle", selection="over", line=total, odds=1.9), dict(book="pinnacle", selection="under", line=total, odds=1.9)],
          "spreads": [dict(book="pinnacle", selection="home", line=spread, odds=1.9), dict(book="pinnacle", selection="away", line=spread, odds=1.9)]}
    return dict(home="A", away="B", commence=datetime(2026, 10, 20, 18, 0), markets=mk)


class KV(dict):
    def get_(self, k):
        return self.get(k)

    def set_(self, k, v):
        self[k] = v


@cleans
def test_parse_ok_and_errors():
    c = leagues_dyn.parse('gre "Greece A1" tid=250 total=160 match=greece+a1 not=youth,reserve fmt=4x10')
    assert c["code"] == "gre" and c["name"] == "Greece A1" and c["oddspapi_tid"] == 250
    assert c["match"] == [["greece", "a1"]] and c["exclude"] == ["youth", "reserve"] and c["fmt"] == [4, 10]
    for bad in ("", "gre", 'g "X" tid=1', 'gre "X"', 'gre "X" tid=abc', 'gre "X" tid=1 fmt=3x10', 'gre "X tid=1'):
        with pytest.raises(ValueError):
            leagues_dyn.parse(bad)


@cleans
def test_apply_merges_in_place_and_remove_undoes():
    cfg = leagues_dyn.parse('gre "Greece A1" tid=250')
    leagues_dyn.apply(cfg["code"], {k: v for k, v in cfg.items() if k != "code"})
    assert "gre" in LEAGUES and "gre" in LEAGUE_KEYS and LEAGUES["gre"]["oddspapi_tid"] == 250 and LEAGUES["gre"]["added"]
    assert _base.league_matches("greece a1 | olympiacos", "gre")
    leagues_dyn.remove_live("gre")
    assert "gre" not in LEAGUES and "gre" not in LEAGUE_KEYS and "gre" not in _base.LEAGUE_RULES


@cleans
def test_builtin_is_never_touched():
    before = dict(LEAGUES["nba"])
    leagues_dyn.apply("nba", dict(name="Hacked", oddspapi_tid=1))
    assert LEAGUES["nba"] == before


@cleans
def test_save_load_roundtrip_after_restart():
    kv = KV()
    cfg = leagues_dyn.parse('gre "Greece A1" tid=250 total=158')
    leagues_dyn.save_one(kv.get_, kv.set_, cfg)
    assert "gre" in json.loads(kv[leagues_dyn.KEY])
    assert leagues_dyn.load(kv.get_) == ["gre"] and LEAGUES["gre"]["avg_total"] == 158.0
    assert leagues_dyn.delete_one(kv.get_, kv.set_, "gre") and not leagues_dyn.delete_one(kv.get_, kv.set_, "gre")


@cleans
def test_corrupt_store_is_ignored():
    assert leagues_dyn.load(lambda k: "{not json") == []


@cleans
def test_estimate_priors_and_probe_text():
    evs = [_ev(160 + i, -3.0) for i in range(4)]
    est = leagues_dyn.estimate_priors(evs)
    assert 160 <= est["avg_total"] <= 163 and 8 <= est["std_margin"] <= 16 and 1.5 <= est["home_adv"] <= 5
    assert "no fixtures" in leagues_dyn.probe_text(9, [], [], {})
    txt = leagues_dyn.probe_text(9, [{"tournamentName": "Greek A1"}] * 4, evs, {})
    assert "Greek A1" in txt and "markets seen: spreads, totals" in txt and "estimated priors" in txt


def _bot(monkeypatch, kv, probe):
    from services import telegram_bot as tb
    monkeypatch.setattr(tb.repo, "kv_get", kv.get_)
    monkeypatch.setattr(tb.repo, "kv_set", kv.set_)
    beast = SimpleNamespace(paused=False, pinref=SimpleNamespace(probe=probe))
    return tb.TelegramBot(beast)


@cleans
def test_bot_addleague_probe_and_remove(monkeypatch):
    kv = KV()
    evs = [_ev(165 + i, -4.0) for i in range(4)]
    bot = _bot(monkeypatch, kv, lambda tid: ([{"tournamentName": "Greek A1"}] * 4, evs, {}))
    assert "4 fixtures" in bot.handle_text("/probe 250")
    out = bot.handle_text('/addleague gre "Greece A1" tid=250')
    assert out.startswith("Added gre") and "Priors estimated" in out and "gre" in LEAGUES and leagues_dyn.KEY in kv
    assert "gre" in bot.handle_text("/leagues") and "(added)" in bot.handle_text("/leagues")
    assert "built-in" in bot.handle_text('/addleague nba "X" tid=1')
    assert bot.handle_text("/removeleague gre").startswith("Removed") and "gre" not in LEAGUES
    assert "built in" in bot.handle_text("/removeleague nba")
    assert "Usage" in bot.handle_text("/probe abc")


@cleans
def test_bot_probe_failure_is_plain_text(monkeypatch):
    def boom(tid):
        raise RuntimeError("OddsPapi daily call cap reached (200/200)")
    bot = _bot(monkeypatch, KV(), boom)
    assert "cap reached" in bot.handle_text("/probe 250")
    out = bot.handle_text('/addleague gre "Greece A1" tid=250')
    assert out.startswith("Added gre") and "default priors" in out


def test_command_menu_matches_the_code():
    from services import telegram_bot as tb
    names = [c for c, _ in tb.COMMANDS]
    assert len(names) == len(set(names)) <= 100
    for c, d in tb.COMMANDS:
        assert c == c.lower() and c.replace("_", "").isalnum() and 3 <= len(d) <= 256
        assert hasattr(tb.TelegramBot, "cmd_" + c), c
    for attr in dir(tb.TelegramBot):                       # a command added in code must reach the menu
        if attr.startswith("cmd_"):
            assert attr[4:] in names, attr


@cleans
def test_find_tournaments_and_command(monkeypatch):
    rows = [dict(tournamentId=250, tournamentName="NBL", categoryName="Australia", liveFixtures=1, upcomingFixtures=2, futureFixtures=4),
            dict(tournamentId=11, tournamentName="A1", categoryName="Greece"), "junk", dict(name="x")]
    txt = leagues_dyn.find_tournaments(rows, "australia nbl")
    assert "tid=250" in txt and "(7 fixtures)" in txt  # live + next 24h + later and "Greece" not in txt
    assert "No OddsPapi" in leagues_dyn.find_tournaments(rows, "narnia")
    assert "tid=11" in leagues_dyn.find_tournaments(rows, "greece")
    bot = _bot(monkeypatch, KV(), None)
    bot.beast.pinref.tournaments = lambda: rows
    assert "tid=250" in bot.handle_text("/tournaments australia")
    assert bot.handle_text("/tournaments").startswith("Usage")
    def boom():
        raise RuntimeError("cap reached")
    bot.beast.pinref.tournaments = boom
    assert "Lookup failed: cap reached" in bot.handle_text("/tournaments x")


def test_tournament_search_matches_whole_words_and_ranks_busiest_first():
    rows = [dict(tournamentId=1, tournamentName="Angolian League", categoryName="Angola", futureFixtures=0),
            dict(tournamentId=2, tournamentName="NBA G League", categoryName="USA", futureFixtures=40),
            dict(tournamentId=3, tournamentName="NBL", categoryName="Australia", futureFixtures=148),
            dict(tournamentId=4, tournamentName="NBL1", categoryName="Australia", futureFixtures=0)]
    t = leagues_dyn.find_tournaments(rows, "g league")
    assert "tid=2" in t and "tid=1" not in t
    t = leagues_dyn.find_tournaments(rows, "australia")
    assert t.index("tid=3") < t.index("tid=4")
    assert "tid=1" in leagues_dyn.find_tournaments(rows, "ang")      # 3+ letters may start a word


def test_nbl_gets_the_verified_oddspapi_extra_tournament():
    assert LEAGUES["nbl"]["oddspapi_extra_tids"] == (1524,) and LEAGUES["nbl"]["odds_api_key"] == "basketball_nbl"


ROWS = [dict(tournamentId=1, tournamentName="NBA G League", categoryName="USA", futureFixtures=40),
        dict(tournamentId=2, tournamentName="NBA G League Showcase Cup", categoryName="USA", futureFixtures=5),
        dict(tournamentId=3, tournamentName="Basketbol Super Ligi", categoryName="Turkey", futureFixtures=0),
        dict(tournamentId=4, tournamentName="B League Premier", categoryName="Japan", futureFixtures=695),
        dict(tournamentId=5, tournamentName="B League One", categoryName="Japan", futureFixtures=660),
        dict(tournamentId=6, tournamentName="B League Next", categoryName="Japan", futureFixtures=12),
        dict(tournamentId=9, tournamentName="W League Premier", categoryName="Japan", futureFixtures=112),
        dict(tournamentId=8, tournamentName="NBA G League Tip-Off Tournament", categoryName="USA", futureFixtures=217),
        dict(tournamentId=7, tournamentName="Basketball Africa League", categoryName="International", futureFixtures=9)]


def _restore(code):
    for k in ("oddspapi_tid", "linked"):
        LEAGUES[code].pop(k, None)


def test_link_candidates_follow_the_rules_and_never_guess():
    assert [c[0] for c in leagues_dyn.link_candidates(ROWS, "g_league")] == [1]       # showcase cup excluded
    assert [c[0] for c in leagues_dyn.link_candidates(ROWS, "bsl")] == [3]
    assert [c[0] for c in leagues_dyn.link_candidates(ROWS, "japan_b1")] == [4]        # Premier = old B1; One / Next excluded
    assert [c[0] for c in leagues_dyn.link_candidates(ROWS, "bal")] == [7]


@cleans
def test_linkall_links_only_when_unambiguous_and_verified():
    kv = KV()
    probed = []

    def probe(tid):
        probed.append(tid)
        return [{}], ([_ev(160, -3.0)] if tid != 7 else []), {}
    try:
        txt = leagues_dyn.linkall(ROWS, probe, kv.get_, kv.set_)
        assert "g_league: LINKED" in txt and LEAGUES["g_league"]["oddspapi_tid"] == 1
        assert "bsl: NOT LINKED" in txt and "off-season" in txt and 3 not in probed          # no games -> no probe, no link
        assert "japan_b1: LINKED" in txt and LEAGUES["japan_b1"]["oddspapi_tid"] == 4
        assert "bal: NOT LINKED" in txt and "no Pinnacle prices" in txt and "oddspapi_tid" not in LEAGUES["bal"]
        assert json.loads(kv[leagues_dyn.LINKS_KEY]) == {"g_league": 1, "japan_b1": 4}
        _restore("g_league")
        _restore("japan_b1")
        assert sorted(leagues_dyn.load_all_links(kv.get_)) == ["g_league", "japan_b1"] and LEAGUES["g_league"]["oddspapi_tid"] == 1   # survives restart
        assert "already has a Pinnacle source" in leagues_dyn.linkall(ROWS, probe, kv.get_, kv.set_).split("\n")[0]
    finally:
        for c in ("g_league", "bsl", "japan_b1", "bal"):
            _restore(c)


@cleans
def test_a_link_never_overrides_an_existing_source():
    assert leagues_dyn.apply_link("nba", 999) is False and LEAGUES["nba"].get("oddspapi_tid") is None
    assert leagues_dyn.apply_link("acb", 999) is False and LEAGUES["acb"]["oddspapi_tid"] == 264


@cleans
def test_bot_linkall(monkeypatch):
    kv = KV()
    bot = _bot(monkeypatch, kv, lambda tid: ([{}], [_ev(160, -3.0)], {}))
    bot.beast.pinref.tournaments = lambda: ROWS
    try:
        assert "g_league: LINKED" in bot.handle_text("/linkall")
        bot.beast.pinref.tournaments = lambda: (_ for _ in ()).throw(RuntimeError("cap reached"))
        assert "Lookup failed: cap reached" in bot.handle_text("/linkall")
    finally:
        for c in ("g_league", "bsl", "japan_b1", "bal"):
            _restore(c)


@cleans
def test_auto_link_is_quiet_unless_something_new_links(monkeypatch):
    from core import beast as bm
    import core.telegram_notify as tn
    sent = []
    kv = KV()
    b = bm.Beast.__new__(bm.Beast)
    b.notify = lambda *a, **k: None
    b.pinref = SimpleNamespace(tournaments=lambda: ROWS, probe=lambda tid: ([{}], [_ev(160, -3.0)] if tid in (1, 4) else [], {}))
    # monkeypatch undoes these after the test: a leaked fake store would link leagues in every later test
    monkeypatch.setattr(bm.repo, "kv_get", kv.get_)
    monkeypatch.setattr(bm.repo, "kv_set", kv.set_)
    monkeypatch.setattr(tn, "send", lambda text, **k: sent.append(text))
    try:
        assert "g_league: LINKED" in b.auto_link() and len(sent) == 1 and "Japan B1" in sent[0] and "NEW PINNACLE SOURCE LINKED" in sent[0]
        sent.clear()
        b.auto_link()                              # bsl / bal still unlinked: nothing new, nothing sent
        assert sent == []
        b.pinref.tournaments = lambda: (_ for _ in ()).throw(RuntimeError("cap"))
        assert b.auto_link() == "" and sent == []
    finally:
        for c in ("g_league", "bsl", "japan_b1", "bal"):
            _restore(c)


def test_scheduler_runs_auto_link_every_few_hours(monkeypatch):
    from core.scheduler import Scheduler, AUTOLINK
    monkeypatch.setenv("AUTOLINK_EVERY_H", "6")
    s = Scheduler(SimpleNamespace(paused=False, run_live=None, run_prematch=None, run_sim=None, auto_link=lambda: "x"))
    every, fn = s._jobs()[AUTOLINK]
    assert every == 6 * 3600 and fn() == "x"
