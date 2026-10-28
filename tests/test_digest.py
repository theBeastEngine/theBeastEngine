"""Hourly scan summary: collecting, formatting, scheduling. No DB, no network."""
import os
import re
import tempfile
from datetime import datetime, timezone

from core import digest
from core.digest import ScanStats, format_digest, near_miss


def plain(t):
    return re.sub(r"</?(b|i)>", "", t)


def nm(ev, p, eid="1", lg="euroleague", market="totals", sel="over", line=163.5):
    return dict(league=lg, event_id=eid, home="Baskonia", away="Dubai", market=market, selection=sel, line=line,
                odds=1.95, ev=ev, p=p)


def ctx(**kw):
    d = dict(alerts_by_mode={"prematch": 1, "sim": 0, "live": 2}, bankroll=100000, placed_today=1, open_bets=2,
             credits="18/30", papi="7/80", warnings=[], keyed={"euroleague", "kbl", "acb"})
    d.update(kw)
    return d


NOW = datetime(2026, 10, 11, 11, 8, tzinfo=timezone.utc)


def test_stats_window_and_reset():
    s = ScanStats()
    s.record_prematch(27, 21, {"euroleague": [7, 5]}, 0, 0, [nm(.031, .53)], [(datetime(2026, 10, 11, 18), "A", "B")])
    s.record_live(4, 1, 6, 0)
    s.record_live(5, 2, 9, 1)
    s.record_sim(27, 21, 12, 0)
    snap = s.snapshot_and_reset()
    assert snap["n_pre"] == 1 and snap["n_live"] == 2 and snap["live_games_max"] == 5 and snap["live_sent"] == 1
    assert snap["pre"]["priced"] == 21 and len(snap["near"]) == 1
    again = s.snapshot_and_reset()
    assert again["n_live"] == 0 and again["near"] == [] and again["pre"]["games"] == 27  # latest scan is kept


def test_peek_does_not_reset():
    s = ScanStats()
    s.record_live(3, 1, 4, 0)
    assert s.snapshot_and_reset(reset=False)["n_live"] == 1
    assert s.snapshot_and_reset(reset=False)["n_live"] == 1
    assert s.snapshot_and_reset()["n_live"] == 1 and s.snapshot_and_reset()["n_live"] == 0


def test_near_miss_keeps_best_per_key():
    s = ScanStats()
    s.add_near([nm(.01, .5), nm(.03, .5)])
    assert len(s.near) == 1 and list(s.near.values())[0]["ev"] == .03


def test_digest_text_quiet_scan(monkeypatch):
    monkeypatch.setenv("ALERT_TZ_OFFSET_HOURS", "1")
    monkeypatch.setenv("DIGEST_EVERY_MIN", "60")
    s = ScanStats()
    s.record_prematch(27, 21, {"euroleague": [7, 5], "kbl": [3, 0]}, 0, 0,
                      [nm(.031, .53), nm(.028, .56, eid="2", lg="lnb", market="spreads", sel="home", line=-4.5)],
                      [(datetime(2026, 10, 11, 18, 5), "Baskonia", "Dubai")])
    s.record_live(4, 1, 6, 0)
    t = plain(format_digest(s.snapshot_and_reset(), ctx(), NOW))
    assert "BEAST SCAN SUMMARY | 11 Oct 12:08 WAT" in t
    assert "Games found: 27 | Priced by Pinnacle: 21 (78%)" in t and "Qualified and sent: 0" in t
    assert "Closest to qualifying (not bets):" in t and "EV +3.1% (needs +4.0%)" in t and "Win prob 53% (needs 55%)" in t and "\u274c" in t
    assert "Live games seen (most at once): 4 | Priced by Pinnacle: 1 | Sent: 0" in t
    assert "ALERTS TODAY: Pre-Match 1 | Sim 0 | Live 2" in t
    assert "BANKROLL ₦100,000 | Placed today 1 | Open bets awaiting /result: 2" in t
    assert "euroleague 5/7" in t and "kbl 0/3" in t
    assert "No Pinnacle price for kbl (3 games)" in t   # keyed league, games found, none priced
    assert "Credits today: Odds API 18/30 | OddsPapi 7/80" in t
    assert "Next priced kick-offs:" in t and "11 Oct 19:05 WAT" in t
    assert "Summary every 60 min" in t and "(not bets)" in t


def test_digest_when_something_was_sent_hides_closest(monkeypatch):
    s = ScanStats()
    s.record_prematch(10, 8, {"nba": [10, 8]}, 2, 2, [nm(.03, .5)], [])
    t = plain(format_digest(s.snapshot_and_reset(), ctx(), NOW))
    assert "Qualified and sent: 2" in t and "Closest to qualifying" not in t


def test_digest_with_nothing_priced_says_so():
    s = ScanStats()
    s.record_prematch(5, 0, {"acb": [5, 0]}, 0, 0, [], [])
    t = plain(format_digest(s.snapshot_and_reset(), ctx(), NOW))
    assert "No game had a Pinnacle price" in t


def test_digest_before_any_scan_does_not_crash():
    t = plain(format_digest(ScanStats().snapshot_and_reset(), ctx(), NOW))
    assert "No Pre-Match scan finished yet" in t and "No Live scan ran." in t and "No Sim scan finished yet." in t


def test_closest_is_one_line_per_game():
    s = ScanStats()
    s.add_near([nm(.03, .5), nm(.02, .5, sel="under"), nm(.025, .5, eid="2")])
    snap = s.snapshot_and_reset()
    assert len(digest._closest(snap["near"])) == 2


def test_every_min_default_and_bad_value(monkeypatch):
    monkeypatch.delenv("DIGEST_EVERY_MIN", raising=False)
    assert digest.every_min() == 60
    monkeypatch.setenv("DIGEST_EVERY_MIN", "abc")
    assert digest.every_min() == 60
    monkeypatch.setenv("DIGEST_EVERY_MIN", "0")
    assert digest.every_min() == 0


def test_pinref_collects_near_misses(monkeypatch):
    from core.pinref import PinRef
    from datetime import timedelta
    p = PinRef(path=os.path.join(tempfile.mkdtemp(), "pr.json"))
    c = (datetime.now(timezone.utc) + timedelta(hours=10)).replace(tzinfo=None)
    p.data["leagues"]["acb"] = {"fetched_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                                "events": [dict(event_id="p1", league="acb", home="Real Madrid", away="FC Barcelona",
                                                commence=c.isoformat(),
                                                markets={"totals": [dict(book="pinnacle", selection="over", line=160.5, odds=1.90),
                                                                    dict(book="pinnacle", selection="under", line=160.5, odds=1.90)]})]}
    ev = dict(event_id="sb1", league="acb", home="Real Madrid", away="FC Barcelona", commence=c,
              markets={"totals": [dict(book="sportybet", selection="over", line=160.5, odds=1.95),
                                  dict(book="sportybet", selection="under", line=160.5, odds=1.80)]})
    near = []
    out = p.candidates(ev, near=near)
    assert out == [] and any(n["selection"] == "over" and n["ev"] < 0.04 for n in near)
    assert near_miss(near[0])["event_id"] == "sb1"


def test_scheduler_has_digest_job(monkeypatch):
    from types import SimpleNamespace
    from core.scheduler import Scheduler, DIGEST
    monkeypatch.setenv("DIGEST_EVERY_MIN", "60")
    called = []
    b = SimpleNamespace(paused=False, run_live=lambda: [], run_prematch=lambda: [], run_sim=lambda: [],
                        send_digest=lambda: called.append(1))
    s = Scheduler(b)
    every, fn = s._jobs()[DIGEST]
    assert every == 3600
    fn()
    assert called == [1]
