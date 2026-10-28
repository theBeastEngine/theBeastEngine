from core import quarters as q


def o(period, elapsed=0.0, hs=0, as_=0, mu=4.0, T=220.0, n=4, L=720, ht=False):
    return q.outlook("nba", period, elapsed, hs, as_, mu, T, n, L, halftime=ht)


def test_only_remaining_quarters_listed():
    assert [x["label"].split()[0] for x in o(1)["quarters"]] == ["Q1", "Q2", "Q3", "Q4"]
    assert [x["label"].split()[0] for x in o(2, 100)["quarters"]] == ["Q2", "Q3", "Q4"]
    assert [x["label"].split()[0] for x in o(3, 100)["quarters"]] == ["Q3", "Q4"]
    assert [x["label"].split()[0] for x in o(4, 100)["quarters"]] == ["Q4"]


def test_halftime_gives_q3_q4():
    r = o(0, 0, 55, 50, ht=True)
    assert [x["label"] for x in r["quarters"]] == ["Q3", "Q4"]


def test_hot_pace_leans_over_cold_leans_under():
    hot = o(2, 360, 70, 62)      # 132 pts at the 3/8 mark of a 220 game
    cold = o(2, 360, 30, 30)
    assert hot["quarters"][0]["total_lean"] == "Over" and cold["quarters"][0]["total_lean"] == "Under"
    assert hot["quarters"][1]["exp_pts"] > cold["quarters"][1]["exp_pts"]


def test_points_add_up_and_current_quarter_is_partial():
    r = o(2, 360, 50, 50)
    assert abs(sum(x["exp_pts"] for x in r["quarters"]) - (0.625 * (0.625 * 220 + 100))) < 1e-6
    assert r["quarters"][0]["label"].endswith("(rest)")


def test_side_lean_and_two_half_league():
    r = q.outlook("ncaab", 1, 0, 0, 0, 10.0, 140.0, 2, 1200)
    assert [x["label"] for x in r["quarters"]] == ["Half 1", "Half 2"]
    assert r["quarters"][0]["side_lean"] == "home"


def test_no_outlook_when_game_over_or_bad_period():
    assert o(4, 720, 100, 90) is None
    assert q.outlook("nba", 9, 0, 0, 0, 0, 220, 4, 720) is None


def test_format_has_forecast_label_and_html():
    r = o(2, 360, 70, 62)
    r.update(home="A & B", away="C", n=4, clock="06:00")
    txt = q.format_outlook([r])
    assert "FORECAST ONLY" in txt and "A &amp; B" in txt and "Q3" in txt and "Q1" not in txt
    assert "\u2B50" not in txt and "side lean" not in txt
    assert q.format_outlook([]).startswith("No live game")


def test_reading_plain_language():
    slow = q.outlook("euroleague", 3, 429, 60, 49, 1.0, 190.0, 4, 600)   # 109 pts after ~27 of 40 min
    slow.update(home="FC Barcelona", away="Valencia Basket", n=4)
    t = q.reading(slow)
    assert "109 points after about 27 of 40 minutes" in t and "slower" in t and "Under" in t
    assert "the rest of Q3 and for Q4" in t
    fast = q.outlook("euroleague", 3, 290, 58, 52, 6.0, 160.0, 4, 600)
    fast.update(home="ASVEL", away="Roanne", n=4)
    t2 = q.reading(fast)
    assert "Over" in t2 and "ASVEL is the stronger side" in t2
    flat = q.outlook("nba", 2, 360, 40, 40, 0.0, 220.0, 4, 720)
    flat.update(home="A", away="B", n=4)
    assert "no scoring lean" in q.reading(flat) and "No clearly stronger side" in q.reading(flat)
    q4 = q.outlook("nba", 4, 100, 90, 85, 0.0, 220.0, 4, 720)
    q4.update(home="A", away="B", n=4)
    assert "the rest of Q4." in q.reading(q4)
