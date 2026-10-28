from config.settings import SIM_TOP_N
from core.market_selector import iter_outcomes
from core.live_model import pregame_params
from core.montecarlo import simulate, prob_home_win, prob_cover, prob_over
from services.ratings import matchup_type


def candidates_sim(event: dict, idx: dict, n: int = 10_000, seed: int = None, params=None) -> list:
    """params = (expected home margin, expected total) from the game's real Pinnacle lines; overrides tier priors."""
    lg = event["league"]
    mt = matchup_type(lg, event["home"], event["away"])
    mu, T, sd = pregame_params(idx, lg, mt)
    if params:
        mu, T = params
    sims = simulate(mu, T, sd, sd * 1.2, n=n, seed=seed)
    out = []
    for o in iter_outcomes(event):
        if o["market"] == "h2h":
            p = prob_home_win(sims)
        elif o["line"] is None:
            continue
        elif o["market"] == "spreads":
            p = prob_cover(sims, o["line"])
        else:
            p = prob_over(sims, o["line"])
        if o["selection"] in ("away", "under"):
            p = 1.0 - p
        if p > 0:
            out.append({**o, "event_id": event["event_id"], "league": lg, "home": event["home"],
                        "away": event["away"], "commence": event.get("commence"), "p": p,
                        "pin_line": (idx.get((mt, o["market"])) or {}).get("line")})
    return out
