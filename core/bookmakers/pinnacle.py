"""Gold-table build: ONE Odds API call per league covers Pinnacle + all soft books,
then derive.py fills the missing Pinnacle markets at 0 credits. Real rows are never overwritten."""
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from config.leagues import LEAGUES, LEAGUE_KEYS
from config.settings import settings, PINNACLE
from core.derive import derive_rows
from services.odds_api import OddsAPI, OutOfCredits, normalize_event, MAIN_MARKETS
from services.ratings import matchup_type
from services.client import HttpError
from services.logger import get_logger
from storage import repository as repo

log = get_logger("gold")


def _upcoming(events, now=None):
    """CLAUDE FIX: the Odds API also returns games already in play; their in-play prices must not enter the
    prematch gold table."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return [e for e in events if not e.get("commence") or e["commence"] > now]


def _pair(ev, book, market):
    outs = [o for o in ev["markets"].get(market, []) if o["book"] == book]
    a_sel, b_sel = ("home", "away") if market in ("h2h", "spreads") else ("over", "under")
    for a in (o for o in outs if o["selection"] == a_sel):
        for b in (o for o in outs if o["selection"] == b_sel):
            if market == "h2h":
                return None, a["odds"], b["odds"]
            if market == "spreads" and a["line"] is not None and a["line"] == b["line"]:  # both are HOME lines
                return a["line"], a["odds"], b["odds"]
            if market == "totals" and a["line"] is not None and a["line"] == b["line"]:
                return a["line"], a["odds"], b["odds"]
    return None


def real_rows_from_events(league, events, books):
    buckets = defaultdict(list)
    for ev in events:
        mt = matchup_type(league, ev["home"], ev["away"])
        for book in books:
            for market in MAIN_MARKETS:
                p = _pair(ev, book, market)
                if p:
                    buckets[(book, mt, market)].append(p)
    rows = []
    for (book, mt, market), vals in buckets.items():
        lines = [v[0] for v in vals if v[0] is not None]
        rows.append(dict(league=league, matchup_type=mt, market=market, source=book,
                         line=(median(lines) if lines else None),
                         side_a_odds=round(median(v[1] for v in vals), 3),
                         side_b_odds=round(median(v[2] for v in vals), 3), is_derived=False))
    return rows


def derive_fill(leagues=None) -> int:
    """0-credit: fill every missing Pinnacle (mt, market) from league priors."""
    n = 0
    for lg in leagues or LEAGUE_KEYS:
        have = {(r["matchup_type"], r["market"]) for r in repo.gold_rows(PINNACLE, lg) if not r["is_derived"]}
        n += repo.upsert_gold(derive_rows(lg, have=have), protect_real=True)
    return n


def build_gold(api=None, leagues=None) -> dict:
    """Paid step (~3 credits per league with an Odds API key). Run once, then daily backup only."""
    api = api or OddsAPI()
    books = [PINNACLE] + list(settings.API_SOFT_BOOKS)
    real, skipped_live, failed = 0, 0, []
    for lg in leagues or LEAGUE_KEYS:
        key = LEAGUES[lg]["odds_api_key"]
        if not key:
            continue
        try:
            raw = api.fetch(key, books=books)
        except OutOfCredits:
            log.error("out of credits at %s; stopping", lg)
            failed.append(lg)
            break
        except HttpError as e:
            log.warning("%s fetch failed: %s", lg, e)
            failed.append(lg)  # CLAUDE FIX: failures are reported in the result, not just logged
            continue
        events = [normalize_event(r, lg) for r in raw]
        upcoming = _upcoming(events)
        skipped_live += len(events) - len(upcoming)
        real += repo.upsert_gold(real_rows_from_events(lg, upcoming, books))
    derived = derive_fill(leagues)
    return dict(real=real, derived=derived, pinnacle_rows=repo.gold_count(PINNACLE),
                failed=failed, skipped_live=skipped_live)
