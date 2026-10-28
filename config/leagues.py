"""14 leagues. avg_total / std_margin / home_adv are PRIORS: tune from real Pinnacle data.
odds_api_key is None where I am not sure The Odds API carries the league; those leagues
get only derived Pinnacle markets until you confirm a key."""
LEAGUES = {
    "nba":       dict(name="NBA", odds_api_key="basketball_nba", oddspapi_extra_tids=(2382, 132),  # 2382 = NBA Preseason, 132 = NBA regular season: quarter / half lines (The Odds API has only full-game lines)
                       avg_total=228.0, std_margin=13.0, home_adv=2.5),
    "g_league":  dict(name="NBA G League", odds_api_key=None, avg_total=232.0, std_margin=14.0, home_adv=2.5),
    "euroleague": dict(name="EuroLeague", odds_api_key="basketball_euroleague", oddspapi_extra_tids=(138,), avg_total=160.0, std_margin=11.0, home_adv=3.5),
    "acb":       dict(name="Spain ACB", odds_api_key=None, oddspapi_tid=264, avg_total=163.0, std_margin=11.0, home_adv=3.5),
    "bsl":       dict(name="Turkey BSL", odds_api_key=None, avg_total=163.0, std_margin=11.5, home_adv=3.5),
    "bbl":       dict(name="Germany BBL", odds_api_key=None, oddspapi_tid=227, avg_total=165.0, std_margin=11.5, home_adv=3.5),
    "lnb":       dict(name="France LNB", odds_api_key=None, oddspapi_tid=156, avg_total=160.0, std_margin=11.0, home_adv=3.5),
    "ncaab":     dict(name="NCAAB", odds_api_key="basketball_ncaab", avg_total=143.0, std_margin=11.5, home_adv=3.8),
    "nbl":       dict(name="Australia NBL", odds_api_key="basketball_nbl", oddspapi_extra_tids=(1524,),  # 1524 = NBL (verified by /probe 2026-10-14: 1st-half lines; The Odds API has full game only)
                       avg_total=185.0, std_margin=12.5, home_adv=3.0),
    "cba":       dict(name="China CBA", odds_api_key=None, oddspapi_tid=1566, avg_total=205.0, std_margin=13.0, home_adv=3.5),
    "japan_b1":  dict(name="Japan B1", odds_api_key=None, avg_total=150.0, std_margin=11.5, home_adv=3.0),
    "kbl":       dict(name="Korea KBL", odds_api_key=None, oddspapi_tid=1540, avg_total=153.0, std_margin=11.5, home_adv=3.0),
    "pba":       dict(name="Philippines PBA", odds_api_key=None, oddspapi_tid=1712, avg_total=190.0, std_margin=12.5, home_adv=2.5),
    "bal":       dict(name="Basketball Africa League", odds_api_key=None, avg_total=160.0, std_margin=12.0, home_adv=3.0),
}
LEAGUE_KEYS = list(LEAGUES)


def pin_source(lg: str):
    """'odds_api' | 'oddspapi' | None: where the Pinnacle price of this league comes from."""
    cfg = LEAGUES.get(lg, {})
    if cfg.get("odds_api_key"):
        return "odds_api"
    if cfg.get("oddspapi_tid"):
        return "oddspapi"
    return None
