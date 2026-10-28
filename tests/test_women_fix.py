"""Portuguese/accented women's leagues must never map to a men's league."""
from core.bookmakers._base import league_matches, infer_league
from core.scores.sofascore import league_of


def test_portuguese_and_accented_women_terms():
    assert not league_matches("Spain Liga Feminina", "acb")
    assert not league_matches("Spain Liga Feminino", "acb")
    assert not league_matches("France LNB Pro A F\u00e9minine", "lnb")
    assert not league_matches("France Betclic Elite F\u00e9minine", "lnb")
    assert infer_league("Portugal Campeonato Nacional Liga Feminina") is None
    assert league_matches("France Betclic Elite", "lnb")
    assert league_matches("Spain Liga Endesa", "acb")


def test_accents_do_not_break_men_leagues():
    assert league_of("Basketbol S\u00fcper Ligi", "T\u00fcrkiye") == "bsl"
    assert league_of("LNB F\u00e9minine", "France") is None
    assert league_of("Liga Feminina", "Spain") is None
    assert league_of("Liga Endesa", "Spain") == "acb"
