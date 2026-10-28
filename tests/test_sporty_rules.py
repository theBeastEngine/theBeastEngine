from core.bookmakers._base import infer_league, league_matches


def test_sportybet_names_seen_in_october_2026_are_recognised():
    assert infer_league("Japan B League Premier") == "japan_b1"            # was missed: no dot in the name
    assert infer_league("Japan B League One") is None                      # the second division
    assert infer_league("Japan B League Next") is None
    assert infer_league("Japan W League Premier") is None                  # women
    assert infer_league("Japan B.League") == "japan_b1"                    # the old spelling still works
    assert infer_league("Turkiye Super Lig") == "bsl"                      # was missed: 'turkiye' is not 'turkey'
    assert infer_league("Turkey Super League") == "bsl"
    assert infer_league("Serbia Super Liga") is None


def test_other_leagues_unchanged():
    assert infer_league("Spain Liga Endesa") == "acb"
    assert infer_league("Greece Basketball League") is None
    assert league_matches("Japan B League Premier", "japan_b1") and not league_matches("Japan B League One", "japan_b1")
