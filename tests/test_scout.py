from core import scout as S

ROWS = [dict(tournamentId=10, tournamentName="Lega Basket Serie A", categoryName="Italy", upcomingFixtures=6, futureFixtures=40),
        dict(tournamentId=11, tournamentName="Serie A2", categoryName="Italy", futureFixtures=30),
        dict(tournamentId=20, tournamentName="A1", categoryName="Greece", futureFixtures=0),
        dict(tournamentId=30, tournamentName="EuroCup", categoryName="International", futureFixtures=20),
        dict(tournamentId=31, tournamentName="EuroCup Women", categoryName="International", futureFixtures=9)]
SPORTY = [("Italy", "Lega Basket Serie A", 8), ("Italy", "Serie A2", 5), ("Greece", "Basket League A1", 4), ("International", "EuroCup Women", 6)]


class FakePR:
    def __init__(self):
        self.probed = []

    def tournaments(self):
        return ROWS

    def probe(self, tid):
        self.probed.append(tid)
        if tid == 10:
            ev = [dict(markets={"h2h": [1], "spreads": [1], "totals": [1], "spreads_q1": [1]})] * 4
            return [1] * 4, ev, {}
        if tid == 30:
            return [1] * 3, [], {}                 # fixtures but no Pinnacle price
        raise RuntimeError("OddsPapi daily call cap reached (200/200)")


def test_papi_matching_excludes_other_divisions_and_women():
    m = S.papi_matches(ROWS, [["italy", "lega"], ["italy", "serie"]], ["women", "a2"])
    assert [x[0] for x in m] == [10]
    assert [x[0] for x in S.papi_matches(ROWS, [["eurocup"]], ["women"])] == [30]


def test_sporty_matching_whole_words_and_excludes():
    assert S.sporty_matches(SPORTY, [["italy", "serie a"]], ["a2"]) == [("Italy", "Lega Basket Serie A", 8)]
    assert S.sporty_matches(SPORTY, [["eurocup"]], ["women"]) == []


def test_run_qualifies_only_with_both_sides_and_gives_an_addleague_line():
    pr = FakePR()
    txt = S.run(pr, sporty=lambda: SPORTY)
    assert "Qualify now: Italy Lega Basket Serie A." in txt
    assert '/addleague ita "Italy Lega Basket Serie A" tid=10' in txt
    assert "no Pinnacle price yet" in txt                      # EuroCup: games but unpriced
    assert "also q1 lines" in txt                              # quarter lines are reported
    assert "Nothing was added" in txt


def test_run_filter_custom_and_errors():
    pr = FakePR()
    assert S.run(pr, "italy", sporty=lambda: SPORTY).count("✅") == 1
    assert "Custom: japan" in S.run(pr, "japan", sporty=lambda: SPORTY)

    class Broken:
        def tournaments(self):
            raise RuntimeError("No OddsPapi key is set on this server.")
    assert "Scout stopped" in S.run(Broken(), sporty=lambda: SPORTY)

    def boom():
        raise RuntimeError("blocked")
    assert "SportyBet feed could not be read" in S.run(FakePR(), sporty=boom)


def test_probe_cap_error_is_reported_not_raised():
    class CapPR(FakePR):
        def probe(self, tid):
            raise RuntimeError("OddsPapi daily call cap reached (200/200)")
    txt = S.run(CapPR(), "italy", sporty=lambda: SPORTY)
    assert "could not check" in txt and "cap reached" in txt


def test_phrase_excludes_and_whole_words_fix_the_first_scout_mistakes():
    rows = [dict(tournamentId=1, tournamentName="Serie B", categoryName="Italy", futureFixtures=559),
            dict(tournamentId=2, tournamentName="Lega Basket Serie A", categoryName="Italy", futureFixtures=40),
            dict(tournamentId=3, tournamentName="British Championship Basketball (BCB) league", categoryName="England", futureFixtures=9),
            dict(tournamentId=4, tournamentName="Champions League", categoryName="International", futureFixtures=30)]
    assert [x[0] for x in S.papi_matches(rows, [["italy", "serie"]], ["women", "serie b"])] == [2]
    assert [x[0] for x in S.papi_matches(rows, [["champions", "league"]], ["women"])] == [4]     # not "Championship"


SB = [("Italy", "Lega Basket Serie A", 8), ("Spain", "ACB", 9), ("Greece", "Basket League", 4), ("Poland", "PLK", 6),
      ("Spain", "Liga Femenina", 3), ("Cyprus", "Division A", 2)]


def test_uncovered_drops_builtin_women_and_youth():
    names = [(c, n) for c, n, _ in S.uncovered(SB)]
    assert ("Spain", "ACB") not in names and ("Spain", "Liga Femenina") not in names
    assert names[0] == ("Poland", "PLK") or names[0] == ("Italy", "Lega Basket Serie A")


def test_run_list_and_auto():
    assert "Poland - PLK (6)" in S.run_list(sporty=lambda: SB)
    rows = [dict(tournamentId=50, tournamentName="PLK", categoryName="Poland", futureFixtures=12),
            dict(tournamentId=51, tournamentName="Lega Basket Serie A", categoryName="Italy", futureFixtures=40)]

    class PR(FakePR):
        def tournaments(self):
            return rows

        def probe(self, tid):
            if tid == 50:
                return [1] * 5, [dict(markets={"h2h": [1], "spreads": [1], "totals": [1]})] * 5, {}
            return [1] * 3, [], {}
    txt = S.run(PR(), "auto", sporty=lambda: SB)
    assert "Qualify now: Poland PLK." in txt and "no Pinnacle price yet" in txt
    assert "/addleague" in txt or "To add" in txt
    assert "Poland - PLK" in S.run(PR(), "list", sporty=lambda: SB)


def test_addleague_line_uses_country_and_name_words():
    v, lines = S.assess("Israel Super League", "isr", [(1197, "Israel", "Super League", 3)], ([1], [dict(markets={"h2h": [1]})], {}),
                        [("Israel", "Super League", 3)])
    assert v == "OK" and any("match=israel+super+league" in l for l in lines)
