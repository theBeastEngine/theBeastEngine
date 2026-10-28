"""Automatic grading: every pick of a game finished, scored from the FINAL score, and a real win-rate report.

Source: The Odds API /scores (2 credits per league per run) for the leagues it covers (nba, euroleague, ncaab, nbl).
It gives the final score of the whole game (overtime included), so full-game markets (moneyline, spread, total) are
graded automatically; quarter / half markets and leagues without a score source stay for /score and /result.
* A PLACED pick is settled for real (bankroll moves, a short message is sent).
* Every other pick is graded in the background only (kv 'auto_grades') so the win rate covers ALL picks the engine sent.
Settling uses core/settle.py (the same rules as /score). Anything unsure returns None and is left alone.
Env: GRADE=0 turns it off; GRADE_EVERY_MIN (default 60) how often it looks."""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

from core import settle as st
from core.pinref import _same
from services.logger import get_logger

log = get_logger("grader")
KEY = "auto_grades"


def enabled() -> bool:
    return os.environ.get("GRADE", "1").strip() != "0"


def _ts(v):
    """datetime / ISO text -> epoch seconds (naive = UTC), or None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        d = v
    else:
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def final_score(ev: dict):
    """(home, away) of a COMPLETED Odds API score row, else None."""
    if not ev.get("completed"):
        return None
    sc = {str(x.get("name")): x.get("score") for x in (ev.get("scores") or [])}
    try:
        return int(float(sc[ev["home_team"]])), int(float(sc[ev["away_team"]]))
    except (KeyError, TypeError, ValueError):
        return None


def grade_pick(pick: dict, home: int, away: int):
    """'won' | 'lost' | 'void' | None. One period = the whole game, so only full-game markets can be graded here."""
    try:
        return st.settle(pick, [(home, away)], None)
    except Exception as e:
        log.warning("could not grade pick %s: %s", pick.get("id"), e)
        return None


class Grader:
    def __init__(self, beast=None, load=None, save=None, now=None, repo=None, send=None, scores=None):
        self.beast = beast
        self._load, self._save = load, save
        self._now = now or time.time
        self._repo_obj = repo
        self._send = send
        self._scores = scores        # test hook: fn(league, days_from) -> list of Odds API score rows
        self.grades = None
        self.finals = None           # core/finals.Finals: last live score of games no score API covers
        self.shadow = None           # core/shadow.Shadow: paper picks graded by the same loop

    def _repo(self):
        if self._repo_obj is None:
            from storage import repository as r
            self._repo_obj = r
        return self._repo_obj

    def _grades(self) -> dict:
        if self.grades is None:
            try:
                blob = self._load() if self._load else None
                self.grades = json.loads(blob) if blob else {}
            except Exception:
                self.grades = {}
        return self.grades

    def _flush(self):
        try:
            if self._save and self.grades is not None:
                self._save(json.dumps(self.grades))
        except Exception as e:
            log.warning("grades not saved: %s", e)

    def _fetch(self, league, days):
        if self._scores:
            return self._scores(league, days)
        from config.leagues import LEAGUES
        from services.odds_api import OddsAPI
        pr = self.beast.pinref
        key = LEAGUES.get(league, {}).get("odds_api_key")
        if not key:
            return None
        if pr.spent_today() + 2 > pr.daily_cap:
            log.info("grader: credit cap reached, scores for %s later", league)
            return None
        if pr._api is None:
            pr._api = OddsAPI()
        rows = pr._api.scores(key, days)
        pr._add_spend(2)
        pr._save()
        return rows

    def tick(self):
        if not enabled():
            return 0
        from config.leagues import LEAGUES, pin_source
        repo = self._repo()
        grades = self._grades()
        now = self._now()
        todo = {}
        for p in repo.all_picks(1500):
            if p.get("status") in ("won", "lost", "void") or str(p.get("id")) in grades:
                continue
            lg = p.get("league")
            if not LEAGUES.get(lg, {}).get("odds_api_key") or pin_source(lg) == "oddspapi":
                continue
            t = _ts(p.get("commence"))
            if t is None or t > now - 3 * 3600 or t < now - 3 * 86400:
                continue
            todo.setdefault(lg, []).append((p, t))
        for p in (self.shadow.pending() if self.shadow else []):
            lg = p.get("league")
            if not LEAGUES.get(lg, {}).get("odds_api_key") or pin_source(lg) == "oddspapi":
                continue
            t = _ts(p.get("commence"))
            if t is None or t > now - 3 * 3600 or t < now - 3 * 86400:
                continue
            todo.setdefault(lg, []).append((p, t))
        done = 0
        for lg, rows in todo.items():
            days = max(1, min(3, math.ceil((now - min(t for _p, t in rows)) / 86400)))
            try:
                events = self._fetch(lg, days)
            except Exception as e:
                log.warning("grader: scores for %s failed: %s", lg, e)
                continue
            for p, t in rows:
                for ev in events or []:
                    fs = final_score(ev)
                    if fs is None or not (_same(p["home"], ev["home_team"]) and _same(p["away"], ev["away_team"])):
                        continue
                    et = _ts(ev.get("commence_time"))
                    if et is not None and abs(et - t) > 24 * 3600:
                        continue
                    res = grade_pick(p, *fs)
                    if res is None:
                        break
                    done += self._record(p, res, fs)
                    break
        done += self._tick_finals(now)
        if done:
            self._flush()
            log.info("grader: %d pick(s) graded", done)
        return done

    def _tick_finals(self, now) -> int:
        """Picks of leagues without a score API, graded from the last live score the engine saw (core/finals.py)."""
        if not self.finals:
            return 0
        from config.leagues import LEAGUES, pin_source
        from core import finals as F
        grades, done = self._grades(), 0
        cands = [p for p in self._repo().all_picks(1500)
                 if p.get("status") not in ("won", "lost", "void") and str(p.get("id")) not in grades]
        cands += self.shadow.pending() if self.shadow else []
        for p in cands:
            lg = p.get("league")
            if LEAGUES.get(lg, {}).get("odds_api_key") and pin_source(lg) != "oddspapi":
                continue                                          # the score API grades these
            t = _ts(p.get("commence"))
            if t is None or t > now - 3600 or t < now - 3 * 86400:
                continue
            fin = self.finals.lookup(lg, p.get("home"), p.get("away"), t)
            res = F.grade(p, fin) if fin else None
            if res:
                done += self._record(p, res[0], res[1])
        return done

    def _record(self, p, res, fs) -> int:
        if p.get("shadow"):
            self.shadow.set_result(p["id"], res, "%d-%d" % fs)
            return 1
        repo = self._repo()
        grades = self._grades()
        grades[str(p["id"])] = dict(res=res, t=int(self._now()), score="%d-%d" % fs)
        if p.get("status") == "placed":
            try:
                new = repo.settle_pick(p["id"], res)
                self._tell(p, res, fs, new)
            except Exception as e:
                log.warning("auto-settle of #%s failed: %s", p.get("id"), e)
        return 1

    def _tell(self, p, res, fs, bankroll):
        import html
        odds = p.get("took_odds") or p.get("book_odds") or 0
        stake = p.get("stake") or 0
        profit = stake * (odds - 1) if res == "won" else (-stake if res == "lost" else 0)
        icon = {"won": "✅", "lost": "❌", "void": "↩️"}[res]
        from core import telegram_notify as tn
        text = "%s <b>BET SETTLED (auto)</b>\n#%s %s vs %s\n%s %s %s · final %d-%d\n<b>%s</b> · %s₦%s · bankroll ₦%s" % (
            icon, p["id"], html.escape(str(p["home"])), html.escape(str(p["away"])), p.get("market"), p.get("selection"),
            "" if p.get("line") is None else p.get("line"), fs[0], fs[1], res.upper(), "+" if profit >= 0 else "-",
            format(int(abs(profit)), ","), format(int(bankroll), ","))
        (self._send or tn.send)(text)


# ---------------------------------------------------------------- report (pure)
def winrate_rows(picks: list, grades: dict) -> list:
    """[(label, n, wins, win_pct, expected_pct, roi_pct)] for each mode and for all picks. Void picks are not counted.
    expected = average of 1/fair_odds (the probability the engine believed); roi = flat 1 unit at the price taken."""
    groups = {}
    for p in picks:
        res = p.get("status") if p.get("status") in ("won", "lost") else (grades.get(str(p.get("id"))) or {}).get("res")
        if res not in ("won", "lost"):
            continue
        odds = p.get("took_odds") or p.get("book_odds")
        fair = p.get("fair_odds")
        if not odds or not fair:
            continue
        for label in ("All picks", str(p.get("mode")).capitalize()):
            g = groups.setdefault(label, dict(n=0, w=0, exp=0.0, ret=0.0))
            g["n"] += 1
            g["w"] += res == "won"
            g["exp"] += 1.0 / fair
            g["ret"] += (odds - 1) if res == "won" else -1.0
    out = []
    for label in ("All picks", "Prematch", "Live", "Sim"):
        g = groups.get(label)
        if g and g["n"]:
            out.append((label, g["n"], g["w"], 100.0 * g["w"] / g["n"], 100.0 * g["exp"] / g["n"], 100.0 * g["ret"] / g["n"]))
    return out
