"""Soft-book odds feed. Event shape = services.odds_api.normalize_event output.
Default source: native scrapers via services.scraper (all return [] until real scrapers are plugged in).
Extra sources: register(SoftAdapter) with upcoming(league) / live(league) / live_game(league, home, away)."""
import asyncio
import concurrent.futures
import re
from core.bookmakers import clean_rows
from services.logger import get_logger

log = get_logger("soft_feed")

_ADAPTERS = []


class SoftAdapter:
    book = "base"

    def upcoming(self, league: str) -> list:
        return []

    def live(self, league: str) -> list:
        return []


def register(adapter: SoftAdapter) -> None:
    _ADAPTERS.append(adapter)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _run(coro):
    """Run a coroutine from sync code (scheduler/bot threads), even if a loop is already running here."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result()


_CANON = {"ML": "h2h", "Spread": "spreads", "Total": "totals"}  # adapter names -> engine names
for _p in ("q1", "q2", "q3", "q4", "h1", "h2", "q4ot", "h2ot"):
    _CANON["Spread_" + _p] = "spreads_" + _p
    _CANON["Total_" + _p] = "totals_" + _p


def _home_line(r):
    """Engine convention: a spread row's line is ALWAYS the home line. Adapters give the away side
    its own line (+2.5), so flip it back here (same rule as services.odds_api)."""
    ln = r.get("line")
    if ln is not None and str(r["market"]).lower().startswith("spread") and r["side"] == "away":
        return -ln
    return ln


def rows_to_events(rows, league: str) -> list:
    ev = {}
    for r in clean_rows(rows):
        h, a = r.get("home"), r.get("away")
        if not h or not a:
            continue
        e = ev.setdefault(norm(h) + "|" + norm(a), dict(
            event_id=str(r.get("match_id") or f"{h}|{a}"), league=league, home=h, away=a,
            commence=r.get("commence"), markets={}))
        e["markets"].setdefault(_CANON.get(r["market"], r["market"]), []).append(
            dict(book=r["book"], selection=r["side"], line=_home_line(r), odds=r["odds"]))
    return list(ev.values())


class ScraperAdapter(SoftAdapter):
    book = "native"

    def upcoming(self, league):
        from services.scraper import scrape_prematch_soft
        return rows_to_events(_run(scrape_prematch_soft(league)), league)

    def live_game(self, league, home, away):
        from services.scraper import scrape_live_soft
        return rows_to_events(_run(scrape_live_soft(league, home, away)), league)


def _merge(events):
    out = {}
    for ev in events:
        k = norm(ev["home"]) + "|" + norm(ev["away"])
        if k not in out:
            out[k] = {**ev, "markets": {m: list(v) for m, v in ev["markets"].items()}}
        else:
            for m, v in ev["markets"].items():
                out[k]["markets"].setdefault(m, []).extend(v)
    return list(out.values())


def _failed(a, what, league, e):
    # CLAUDE FIX: a broken adapter used to be swallowed silently, so a dead feed looked like "no value today".
    log.warning("soft adapter %s %s(%s) failed: %s: %s", getattr(a, "book", type(a).__name__), what, league,
                type(e).__name__, e)


def upcoming(league: str) -> list:
    evs = []
    for a in _ADAPTERS:
        try:
            evs.extend(a.upcoming(league) or [])
        except Exception as e:
            _failed(a, "upcoming", league, e)
    return _merge(evs)


def live_game(league: str, home: str, away: str) -> list:
    evs = []
    for a in _ADAPTERS:
        try:
            fn = getattr(a, "live_game", None)
            evs.extend((fn(league, home, away) if fn else a.live(league)) or [])
        except Exception as e:
            _failed(a, "live_game", league, e)
    return _merge(evs)


def live(league: str) -> list:  # kept for older callers
    evs = []
    for a in _ADAPTERS:
        try:
            evs.extend(a.live(league) or [])
        except Exception as e:
            _failed(a, "live", league, e)
    return _merge(evs)


register(ScraperAdapter())
