"""Alert templates: Prematch / Live / Sim. Pure formatting, no DB or network."""
import re
from core.telegram_notify import format_pick


def pk(**kw):
    d = dict(id=7, mode="prematch", league="nba", home="Boston", away="Miami", market="spreads",
             selection="home", line=-2.5, book="sportybet", odds=1.95, fair_odds=1.85, min_odds=1.91,
             ev=0.05, p=0.55, stake=5000.0, commence_time="2026-10-20T00:30:00Z")
    d.update(kw)
    return d


def plain(t):
    return re.sub(r"</?(b|i)>", "", t)


def test_prematch_template(monkeypatch):
    monkeypatch.setenv("ALERT_TZ_OFFSET_HOURS", "1")
    raw = format_pick(pk())
    t = plain(raw)
    assert "PREMATCH VALUE  #7" in t and "<b>PREMATCH VALUE</b>" in raw
    assert "NBA  \u00b7  \U0001F550 20 Oct 01:30 WAT" in t and "Boston vs Miami" in t
    assert "BET: Spread Boston -2.5" in t and "<b>BET: Spread Boston -2.5</b>" in raw
    assert "sportybet @ 1.95   (fair 1.85)" in t and "@ <b>1.95</b>" in raw
    assert "Min odds to take: 1.91" in t and "Min odds to take: <b>1.91</b>" in raw
    assert "EV: +5.0%" in t and "Win prob: 55.0%" in t and "EV: <b>+5.0%</b>" in raw
    assert "Stake: \u20a65,000" in t and "Valid for" not in t
    assert "Type of Match: Pre-Match" in t


def test_live_template():
    t = plain(format_pick(pk(mode="live", market="totals", selection="over", line=220.5, period=3,
                             remaining_sec=252, home_score=54, away_score=50)))
    assert "LIVE VALUE  #7" in t
    assert "NBA  \u00b7  \u23f1 Q3 4:12 left" in t and "Boston 54 - 50 Miami" in t
    assert "BET: Total Over 220.5" in t and "Valid for ~2 min" in t and "Type of Match: Live Match" in t


def test_live_ncaab_uses_halves_and_clock_string():
    t = plain(format_pick(pk(mode="live", league="ncaab", period=2, clock="7:30", home_score=30, away_score=28)))
    assert "NCAAB" in t and "H2 7:30 left" in t


def test_live_without_score_falls_back_to_teams():
    t = plain(format_pick(pk(mode="live")))
    assert "Boston vs Miami" in t and "left" not in t


def test_missing_optional_fields_do_not_crash():
    t = plain(format_pick(pk(id=None, p=None, commence_time=None)))
    assert "#" not in t and "Win prob" not in t and t.splitlines()[2].endswith("NBA")


def test_names_are_html_escaped():
    t = format_pick(pk(home="A&B <x>"))
    assert "A&amp;B &lt;x&gt;" in t and "<x>" not in t


def test_win_probability_colour_dot():
    assert "\u2705" in format_pick(pk(p=0.70))
    assert "\U0001F536" in format_pick(pk(p=0.60))
    assert "\U0001F538" in format_pick(pk(p=0.55))


def test_sim_alert_carries_model_warning():
    assert "Model-based estimate" in format_pick(pk(mode="sim"))
    assert "Model-based estimate" not in format_pick(pk(mode="prematch"))
