"""Pinnacle reference prices for tennis, from The Odds API (tennis_* sport keys) and OddsPapi (tennis tournaments).
Matches are joined to SportyBet by PLAYER NAMES + START TIME (never by tournament name; the players may be listed in the
opposite order, which is detected and corrected), then checked again by the match-winner price agreement (model.agree).
Credits are spent only for tournaments that really have a SportyBet match to compare."""
from __future__ import annotations

import json
import re
import logging
import time
from datetime import datetime, timezone

from core.devig import devig
from tennis import cfg, model
from tennis.names import pair_score, bad_league, similarity, tokens
from services.client import get_json, HttpError
from services import odds_api as oa
from services.oddspapi import side_market

log = logging.getLogger("tennis.pin")
TIME_TOL_S = 4 * 3600          # tennis start times are estimates; the player names are the real key
MIN_SCORE = 0.84
PAPI_TENNIS = 12


def _epoch(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------ The Odds API
def normalize_oa(raw: dict, book: str = "pinnacle") -> dict:
    """One The Odds API event -> {id, home, away, t, markets} from `book` only (full time, 90 minutes)."""
    home, away = raw.get("home_team"), raw.get("away_team")
    ev = dict(id=str(raw.get("id")), home=home, away=away, t=_epoch(raw.get("commence_time")), markets={})
    for b in raw.get("bookmakers") or []:
        if b.get("key") != book:
            continue
        for m in b.get("markets") or []:
            key = m.get("key")
            for o in m.get("outcomes") or []:
                name, pt, px = o.get("name"), o.get("point"), o.get("price")
                try:
                    px = float(px)
                except (TypeError, ValueError):
                    continue
                if px <= 1.0:
                    continue
                if key == "h2h":
                    sel = "home" if name == home else ("away" if name == away else None)
                    if sel:
                        model.ladder_put(ev, "ml", None, sel, px)
                elif key == "totals" and str(name).lower() in ("over", "under") and pt is not None:
                    model.ladder_put(ev, "total", float(pt), str(name).lower(), px)
                elif key == "spreads" and pt is not None:
                    if name == home:
                        model.ladder_put(ev, "ah", float(pt), "home", px)
                    elif name == away:
                        model.ladder_put(ev, "ah", -float(pt), "away", px)
    return ev


def oa_call(client, path: str, params: dict):
    """A The Odds API call that rotates keys like OddsAPI.fetch does. Returns (data, headers)."""
    retries = 0
    while True:
        key = client._key()
        try:
            return get_json(oa.BASE + path, dict(params, apiKey=key))
        except HttpError as e:
            if e.status == 429 and retries < oa.MAX_429_RETRIES:
                retries += 1
                time.sleep(2 * retries)
                continue
            if e.status in (401, 429):
                log.warning("Odds API key ...%s %s: %s", key[-4:], e.status, str(e).replace(key, "***")[:120])
                client._dead(key, e.status)
                continue
            raise HttpError(e.status, str(e).replace(key, "***"))


# ------------------------------------------------------------------ OddsPapi tennis
_SKIP = ("set", "tiebreak", "tie-break", "tie break", "ace", "fault", "break", "point", "player", "odd", "even", "exact", "range",
         "race", "correct", "highest", "most", "specials", "first", "second", "third", "1st", "2nd", "3rd", "4th", "5th", "next",
         "live", "each", "both", "winning margin", "double fault")
_FULL = ("result", "fulltime", "full time", "ft", "match", "game", "regular time", "regulartime", "")


def classify_tn(cat: dict):
    """OddsPapi catalog entry -> ('ml' | 'ah' | 'total', handicap) for whole-match markets only, else None."""
    if not cat or cat.get("playerProp"):
        return None
    mt = str(cat.get("marketType", "")).lower()
    onames = {str(o.get("outcomeName", "")).strip() for o in (cat.get("outcomes") or []) if isinstance(o, dict)}
    if str(cat.get("period", "")).lower() == "result" and mt == "correctscore" and onames == {"2:0", "2:1", "0:2", "1:2"}:
        return ("cs", None)               # best-of-3 correct score in sets (the 3:x entry is the best-of-5 one)
    if str(cat.get("period", "")).lower() == "result" and mt == "exactsets" and {"2", "3"} <= onames:
        return ("es", None)               # outcomes 2..5 sets; dropped later for best-of-5 games
    if str(cat.get("marketName", "")).strip().lower() == "set handicap" and str(cat.get("period", "")).lower() == "result" \
            and len(cat.get("outcomes") or []) == 2 and cat.get("handicap") is not None and abs(float(cat["handicap"])) in (1.5, 2.5):
        return ("sh", float(cat["handicap"]))
    if str(cat.get("period", "")).lower().replace(" ", "") not in tuple(x.replace(" ", "") for x in _FULL):
        return None
    text = ("%s %s" % (cat.get("marketType", ""), cat.get("marketName", ""))).lower()
    outs = cat.get("outcomes") or []
    if len(outs) != 2 or any(re.search(r"\b%s\b" % re.escape(w), text) for w in _SKIP):
        return None
    h = cat.get("handicap")
    if h is not None and side_market(cat):
        return None                                       # a player / team market is never a match total or handicap
    if "handicap" in text or "spread" in text:
        return ("ah", float(h)) if h is not None and "game" in text else None
    if "total" in text or "over" in text:
        return ("total", float(h)) if h is not None and ("game" in text or "total" == text.strip()) else None
    if any(w in text for w in ("moneyline", "winner", "match result", "full time result", "to win", "h2h", "1x2")) and (h is None or float(h) == 0.0):
        return ("ml", None)
    return None


def _papi_side(kind, name, idx):
    nm = str(name).lower().strip()
    if kind == "total":
        if nm.startswith("over") or nm == "o":
            return "over"
        if nm.startswith("under") or nm == "u":
            return "under"
        return ("over", "under")[idx] if idx in (0, 1) else None
    if nm in ("1", "home", "h1", "team1", "participant1"):
        return "home"
    if nm in ("2", "away", "h2", "team2", "participant2"):
        return "away"
    return ("home", "away")[idx] if idx in (0, 1) else None


def normalize_papi(fixtures: list, names: dict, catalog: dict, stats: dict = None) -> list:
    """OddsPapi odds-by-tournaments reply -> pin events (Pinnacle full-time match winner, Asian handicap, totals)."""
    stats = stats if stats is not None else {}
    out = []
    for fx in fixtures or []:
        bo = ((fx.get("bookmakerOdds") or {}).get("pinnacle") or {})
        home = names.get(str(fx.get("participant1Id"))) or fx.get("participant1Name")
        away = names.get(str(fx.get("participant2Id"))) or fx.get("participant2Name")
        if not home or not away:
            stats["no_names"] = stats.get("no_names", 0) + 1
            continue
        ev = dict(id="papi-%s" % fx.get("fixtureId"), home=home, away=away, t=_epoch(fx.get("startTime")), markets={})
        for mid, m in (bo.get("markets") or {}).items():
            try:
                cat = catalog.get(int(mid))
            except (TypeError, ValueError):
                cat = None
            if not cat:
                stats.setdefault("unknown_ids", set()).add(str(mid))
                continue
            cl = classify_tn(cat)
            if not cl:
                stats.setdefault("skipped", {})[str(cat.get("marketType"))] = 1
                continue
            kind, hcap = cl
            if kind == "total" and not (10.5 <= hcap <= 50.5):
                continue
            if kind in ("cs", "es"):
                order = [o.get("outcomeId") for o in cat.get("outcomes", [])]
                nm_by = {o.get("outcomeId"): str(o.get("outcomeName", "")).strip() for o in cat.get("outcomes", [])}
                for oid, o in (m.get("outcomes") or {}).items():
                    pl = (o.get("players") or {}).get("0") or next(iter((o.get("players") or {}).values()), None)
                    try:
                        price = float((pl or {}).get("price"))
                        label = nm_by.get(int(oid), "")
                    except (TypeError, ValueError):
                        continue
                    if not pl or pl.get("active") is False or price <= 1.0:
                        continue
                    if kind == "cs":
                        model.ladder_put(ev, "cs", None, label, price)
                    elif label in ("2", "3"):
                        model.ladder_put(ev, "es", None, "s" + label, price)
                    elif label in ("4", "5"):
                        ev["_bo5"] = True      # priced 4 or 5 sets: a best-of-5, never compared with SportyBet's 2/3 market
                continue
            order = [o.get("outcomeId") for o in cat.get("outcomes", [])]
            nm_by = {o.get("outcomeId"): o.get("outcomeName") for o in cat.get("outcomes", [])}
            for oid, o in (m.get("outcomes") or {}).items():
                pl = (o.get("players") or {}).get("0") or next(iter((o.get("players") or {}).values()), None)
                if not pl or pl.get("active") is False:
                    continue
                try:
                    price = float(pl.get("price"))
                except (TypeError, ValueError):
                    continue
                if price <= 1.0:
                    continue
                try:
                    oid_i = int(oid)
                except (TypeError, ValueError):
                    oid_i = oid
                idx = order.index(oid_i) if oid_i in order else -1
                sel = _papi_side(kind, nm_by.get(oid_i, ""), idx)
                if sel:
                    model.ladder_put(ev, kind, hcap, sel, price)
        _fix_ah_sign(ev, stats)
        if ev.pop("_bo5", False):
            ev["markets"].pop("es", None)
        mk = ev["markets"]
        if mk.get("cs") and mk.get("ml") and all(x in mk["cs"] for x in ("2:0", "2:1", "1:2", "0:2")) and all(x in mk["ml"] for x in ("home", "away")):
            pc = devig([mk["cs"][x] for x in ("2:0", "2:1", "1:2", "0:2")])      # the correct score must agree with the match winner
            pm = devig([mk["ml"]["home"], mk["ml"]["away"]])[0]
            if (pc[0] + pc[1] - 0.5) * (pm - 0.5) < 0 and abs(pm - 0.5) > 0.08:
                mk.pop("cs", None)
                stats["cs_dropped"] = stats.get("cs_dropped", 0) + 1
        if ev["markets"]:
            out.append(ev)
    return out


def _fix_ah_sign(ev, stats):
    """The handicap line must be the HOME line (a home favourite is negative). If the main line contradicts the match winner favourite,
    the feed uses the other convention: flip every AH line of the game (draw-no-bet 0 stays 0)."""
    for fam in ("ah", "sh"):
        _fix_sign(ev, stats, fam)


def _fix_sign(ev, stats, fam):
    ah, x = ev["markets"].get(fam), ev["markets"].get("ml")
    if not ah or not x or not all(s in x for s in ("home", "away")):
        return
    best = None
    for ln, d in ah.items():
        if "home" in d and "away" in d:
            gap = abs(d["home"] - d["away"])
            if best is None or gap < best[0]:
                best = (gap, ln)
    if best is None or abs(best[1]) < 0.5:
        return
    home_fav = x["home"] < x["away"]
    if (home_fav and best[1] > 0) or (not home_fav and best[1] < 0):
        ev["markets"][fam] = {round(-ln, 2): d for ln, d in ah.items()}
        stats[fam + "_flipped"] = stats.get(fam + "_flipped", 0) + 1


# ------------------------------------------------------------------ the book
class PinBook:
    """Cache of Pinnacle prices + the join to SportyBet games."""

    def __init__(self, oa_client=None, papi_client=None, load=None, save=None, clock=None):
        self.oa = oa_client
        self.papi = papi_client
        self._load, self._save = load, save
        self._clock = clock or time.time
        self.src = {}          # source id -> {t: fetched epoch, events: [pin events]}
        self.sports = {"t": 0.0, "keys": []}
        self.index = {}        # odds-api sport key -> {t, events: [(id, home, away, t)]}
        self.link = {}         # soft event id -> (source id, pin event id)
        self.nolink_until = {}
        self.dead_tid = {}     # OddsPapi tournament id -> epoch until which it is skipped (answered 'no fixtures')
        self.papi_tours = {"t": 0.0, "rows": []}
        self.papi_catalog = {}
        self.papi_names = {"t": 0.0, "d": {}}
        self.spend = self._read_spend()
        self.diag = {}

    # ---- spend
    def _day(self):
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y%m%d")

    def _read_spend(self):
        try:
            d = json.loads(self._load("tn_pin_spend")) if self._load else {}
        except Exception:
            d = {}
        return d if d.get("day") == self._day() else dict(day=self._day(), oa=0, pp=0)

    def _spent(self, which, n):
        if self.spend.get("day") != self._day():
            self.spend = dict(day=self._day(), oa=0, pp=0)
        self.spend[which] = self.spend.get(which, 0) + n
        try:
            if self._save:
                self._save("tn_pin_spend", json.dumps(self.spend))
        except Exception:
            pass

    def oa_left(self) -> float:
        if self.spend.get("day") != self._day():
            self.spend = dict(day=self._day(), oa=0, pp=0)
        return cfg.f("TN_OA_DAILY_CAP", 600) - self.spend.get("oa", 0)

    def pp_cap(self) -> float:
        cap = cfg.f("TN_PAPI_DAILY_CAP", 120)
        try:
            if getattr(self, "papi", None) is not None and hasattr(self.papi, "daily_allowance"):
                cap = min(cap, self.papi.daily_allowance(self.spend.get("pp", 0) if self.spend.get("day") == self._day() else 0))     # never more than the keys can give over the rest of the month
        except Exception as e:
            log.warning("OddsPapi allowance check failed: %s", e)
        return cap

    def pp_left(self) -> float:
        if self.spend.get("day") != self._day():
            self.spend = dict(day=self._day(), oa=0, pp=0)
        return self.pp_cap() - self.spend.get("pp", 0)

    # ---- The Odds API: which tennis tournaments exist / have matches (both free calls)
    def tennis_keys(self):
        now = self._clock()
        if self.sports["keys"] and now - self.sports["t"] < 12 * 3600:
            return self.sports["keys"]
        data, _ = oa_call(self.oa, "/sports", {})
        keys = [s["key"] for s in data if str(s.get("key", "")).startswith("tennis_") and s.get("active", True)
                and not s.get("has_outrights") and not bad_league(s.get("title", "") + " " + s.get("description", ""))
                and not s["key"].endswith("_winner")]
        self.sports = {"t": now, "keys": keys}
        return keys

    def refresh_index(self, soft_events: list, horizon_s: float):
        """Free: list each tennis tournament's coming matches, join them to SportyBet matches by player names + time."""
        now = self._clock()
        soft = [e for e in soft_events if now - 600 <= e["t"] <= now + horizon_s]
        by_time = sorted(soft, key=lambda e: e["t"])
        keys = self.tennis_keys()
        n_keys = 0
        for k in keys:
            ix = self.index.get(k)
            if ix and now - ix["t"] < 50 * 60:
                continue
            try:
                data, _ = oa_call(self.oa, "/sports/%s/events" % k, {})
            except oa.OutOfCredits:
                raise
            except HttpError as e:
                log.warning("events list failed for %s: %s", k, e)
                continue
            self.index[k] = dict(t=now, events=[(str(x.get("id")), x.get("home_team"), x.get("away_team"), _epoch(x.get("commence_time")))
                                                for x in data if isinstance(x, dict)])
            n_keys += 1
        # join (players may be listed in the opposite order: remember it so prices can be turned around)
        linked = 0
        for se in by_time:
            if se["id"] in self.link:
                continue
            best = None
            for k, ix in self.index.items():
                for (pid, h, a, t) in ix["events"]:
                    if t is None or abs(t - se["t"]) > TIME_TOL_S:
                        continue
                    sc, sw = pair_score(se["home"], se["away"], h or "", a or "")
                    if sc >= MIN_SCORE and (best is None or sc > best[0]):
                        best = (sc, "oa:" + k, pid, sw)
            if best:
                self.link[se["id"]] = (best[1], best[2], best[3])
                linked += 1
        self.diag["index"] = dict(leagues=len(keys), listed=n_keys, linked_now=linked, linked_total=len(self.link))
        return linked

    # ---- pulling prices
    def _pull_oa(self, key: str):
        markets = ("h2h", "spreads", "totals")
        if self.oa_left() < len(markets):
            raise oa.OutOfCredits("Odds API daily budget for tennis reached")
        raw = self.oa.fetch(key, markets=markets, books=["pinnacle"])
        self._spent("oa", len(markets))
        events = [normalize_oa(r) for r in raw if isinstance(r, dict)]
        self.src["oa:" + key] = dict(t=self._clock(), events=[e for e in events if e["markets"]])
        return self.src["oa:" + key]

    def age_min(self, source: str):
        s = self.src.get(source)
        return None if not s else (self._clock() - s["t"]) / 60.0

    def get(self, soft_event: dict, max_age_min: float, force_min_age: float = None):
        """-> (pin event or None, age in minutes or None, note). Pulls the league's prices when the cache is older than
        max_age_min. force_min_age: at the late checks pull again when older than this (never more often)."""
        lk = self.link.get(soft_event["id"])
        if not lk:
            return None, None, "no Pinnacle game found for it"
        source, pid, swapped = (tuple(lk) + (False,))[:3]
        age = self.age_min(source)
        limit = max_age_min if force_min_age is None else min(max_age_min, force_min_age)
        if age is None or age > limit:
            try:
                if source.startswith("oa:"):
                    self._pull_oa(source[3:])
                else:
                    self._pull_pp(source)
            except oa.OutOfCredits as e:
                if age is None:
                    return None, None, str(e)
            except HttpError as e:
                log.warning("pinnacle pull %s failed: %s", source, e)
                if age is None:
                    return None, None, "pull failed (%s)" % str(e)[:60]
            age = self.age_min(source)
        s = self.src.get(source)
        if not s:
            return None, None, "no price cached"
        for e in s["events"]:
            if e["id"] == pid:
                return (model.flip(e) if swapped else e), age, ""
        return None, age, "the league was pulled but this game has no Pinnacle price in it"

    # ---- OddsPapi (leagues The Odds API does not carry)
    def _pull_pp(self, source: str, reserve: float = 0):
        tids = [int(x) for x in source[3:].split(",") if x]
        if self.pp_left() < 3 + reserve:
            raise oa.OutOfCredits("OddsPapi daily budget for tennis reached")
        before = self.papi.calls
        try:
            if not self.papi_catalog:
                cat = self.papi.call("/v4/markets", {"language": "en"})
                self.papi_catalog = {int(m["marketId"]): m for m in (cat if isinstance(cat, list) else [])
                                     if m.get("sportId") in (None, self.papi_sport_id()) and str(m.get("marketId", "")).isdigit()}
                sets = []
                for m in self.papi_catalog.values():
                    t = ("%s %s" % (m.get("marketType", ""), m.get("marketName", ""))).lower()
                    if "set" in t or str(m.get("period", "")).lower() not in ("result", "fulltime", "full time", "ft", "match", "game", ""):
                        sets.append("%s | %s | %s | h=%s | %d out" % (m.get("marketType", ""), m.get("period", ""), str(m.get("marketName", ""))[:34],
                                                                   m.get("handicap"), len(m.get("outcomes") or [])))
                self.diag["papi_set_markets"] = [x for x in sorted(set(sets)) if "|" in x and "correctscore" not in x and "gamewinner" not in x][:14]
                wins = []
                for m in self.papi_catalog.values():
                    if str(m.get("marketType", "")).lower() in ("moneyline", "spreads", "spreads-games", "totals", "totals-games", "1x2"):
                        wins.append("%s | %s | %s | h=%s | %d out -> %s" % (m.get("marketType", ""), m.get("period", ""), str(m.get("marketName", ""))[:28],
                                                                         m.get("handicap"), len(m.get("outcomes") or []), classify_tn(m)))
                wins.sort(key=lambda x: (0 if x.split(" | ")[1] == "result" else 1, x))
                self.diag["papi_winner_markets"] = wins[:45]
                oth = []
                for m in self.papi_catalog.values():
                    mt = str(m.get("marketType", "")).lower()
                    if str(m.get("period", "")).lower() == "result" and mt not in ("moneyline", "spreads", "spreads-games", "totals", "totals-games") \
                            and "gamewinner" not in mt and "deuce" not in mt:
                        oth.append("%s | %s | h=%s | %s" % (mt, str(m.get("marketName", ""))[:26], m.get("handicap"),
                                                          ",".join(str(o.get("outcomeName"))[:12] for o in (m.get("outcomes") or [])[:8])))
                self.diag["papi_other_markets"] = sorted(set(oth))[:40]
            raw = self.papi.odds(tids)
            ids = {str(f.get(k)) for f in raw for k in ("participant1Id", "participant2Id")}
            if ids - set(self.papi_names["d"]) and self._clock() - self.papi_names["t"] > 3600:
                part = self.papi.call("/v4/participants", {"sportId": self.papi_sport_id(), "language": "en"})
                self.papi_names = {"t": self._clock(), "d": {str(k): v for k, v in part.items()} if isinstance(part, dict) else {}}
            stats = {}
            events = normalize_papi(raw, self.papi_names["d"], self.papi_catalog, stats)
            self.diag["papi_stats"] = {k: (sorted(v)[:5] if isinstance(v, (set, dict)) else v) for k, v in stats.items()}
        finally:
            self._spent("pp", max(1, self.papi.calls - before))
        self.src[source] = dict(t=self._clock(), events=events)

    def papi_link(self, soft_events: list):
        """Soft games that The Odds API could not price: find the OddsPapi tournament by country + name, pull it, join games."""
        if self.papi is None:
            return 0
        now = self._clock()
        if not self.papi_tours["rows"] or now - self.papi_tours["t"] > 24 * 3600:
            if self.pp_left() < 1:
                return 0
            try:
                rows = self.papi.call("/v4/tournaments", {"sportId": self.papi_sport_id(), "language": "en"})
            except Exception as e:
                log.warning("OddsPapi tournaments failed: %s", e)
                return 0
            self._spent("pp", 1)
            self.papi_tours = {"t": now, "rows": rows if isinstance(rows, list) else []}
        todo = {}
        link_s = cfg.f("TN_PAPI_LINK_H", 8) * 3600        # only look for Pinnacle prices of games starting soon: the rest can wait
        for se in soft_events:
            if se["id"] in self.link or self.nolink_until.get(se["id"], 0) > now or se["t"] - now > link_s:
                continue
            tids = [t for t in self._tours_for(se) if self.dead_tid.get(t, 0) <= now]
            if not tids:
                self.nolink_until[se["id"]] = now + 3 * 3600
                continue
            for tid in tids:
                todo.setdefault(tid, []).append(se)
        linked = 0
        items = list(todo.items())[:8]
        n = 4 if getattr(self, "single_until", 0) <= now else 1     # four tournaments per OddsPapi call: the budget goes 4x further
        fresh_s = cfg.f("TN_PAPI_FRESH_MIN", 120) * 60
        tour_src = getattr(self, "tour_src", None)
        if tour_src is None:
            tour_src = self.tour_src = {}                 # OddsPapi tournament id -> the cached pull that holds it
        def fresh(t):
            s_ = tour_src.get(t)
            return bool(s_ and s_ in self.src and now - self.src[s_]["t"] < fresh_s)
        for i in range(0, len(items), n):
            chunk = items[i:i + n]
            tids = [t for t, _ in chunk]
            ses_all = [se for _, ses in chunk for se in ses]
            stale = [t for t in tids if not fresh(t)]       # a tournament already pulled lately is never pulled again for linking
            src = "pp:" + ",".join(str(t) for t in stale)
            try:
                if stale:
                    self._pull_pp(src, reserve=min(cfg.f("TN_PAPI_RESERVE", 40), 0.35 * self.pp_cap()))   # the last calls of the day are kept for the T-60..T-5 re-checks
                    for t in stale:
                        tour_src[t] = src
            except Exception as e:
                log.warning("OddsPapi pull %s failed: %s", src, e)
                if "FIXTURE_NOT_FOUND" in str(e) or "No fixtures" in str(e):
                    if len(stale) > 1:                       # maybe the batch is the problem: go one by one for a while
                        self.single_until = now + 3 * 3600
                    else:
                        self.dead_tid[stale[0]] = now + 6 * 3600
                if "budget" in str(e):
                    break
                for se in ses_all:
                    self.nolink_until[se["id"]] = now + (600 if len(stale) > 1 else 3600)
                continue
            srcs = []
            for t in tids:
                if fresh(t) and tour_src[t] not in srcs:
                    srcs.append(tour_src[t])
            for se in ses_all:
                if se["id"] in self.link:
                    continue
                best = None
                for sx in srcs:
                    for e in self.src[sx]["events"]:
                        if e["t"] is None or abs(e["t"] - se["t"]) > TIME_TOL_S:
                            continue
                        sc, sw = pair_score(se["home"], se["away"], e["home"], e["away"])
                        if sc >= MIN_SCORE and (best is None or sc > best[0]):
                            best = (sc, e["id"], sw, sx)
                if best:
                    self.link[se["id"]] = (best[3], best[1], best[2])
                    linked += 1
        for ses in todo.values():
            for se in ses:
                if se["id"] not in self.link:
                    self.nolink_until[se["id"]] = now + 3 * 3600      # Pinnacle does not price it: do not ask again every hour
        return linked

    _TOUR_NOISE = {"atp", "wta", "itf", "challenger", "men", "mens", "women", "womens", "singles", "doubles", "tour", "open", "cup",
                   "masters", "usa", "world", "championships", "international", "grand", "slam", "round", "final"}

    def papi_sport_id(self) -> int:
        """OddsPapi's id for tennis (looked up once from its sport list, 12 when the list cannot be read)."""
        if getattr(self, "_psid", None):
            return self._psid
        sid = PAPI_TENNIS
        try:
            rows = self.papi.call("/v4/sports", {"language": "en"}) if self.papi else []
            for r in rows if isinstance(rows, list) else []:
                nm = str(r.get("sportName", r.get("name", ""))).lower()
                if "tennis" in nm and "table" not in nm:
                    sid = int(r.get("sportId", r.get("id", sid)))
                    break
        except Exception as e:
            log.warning("OddsPapi sport list failed: %s", e)
        self._psid = sid
        return sid

    def _tours_for(self, se):
        """OddsPapi tournament ids whose name shares a distinctive word (usually the city) with the SportyBet tournament.
        Several may qualify (men's and women's events); the players decide the real join, so a wrong candidate only costs a call."""
        n = se.get("tname", "") or ""
        low = n.lower()
        if bad_league(n) or "doubles" in low or "utr" in low.split() or "utr ptt" in low:
            return []                       # Pinnacle does not price doubles or UTR events: no point spending calls on them
        gender = "women" if re.search(r"\bw\d{2}\b|\bwomen\b|\bwta\b", (se.get("lname", "") + " " + n).lower()) else (
            "men" if re.search(r"\bm\d{2}\b|\bmen\b|\batp\b", (se.get("lname", "") + " " + n).lower()) else "")
        mine = {t for t in tokens(n) if len(t) >= 4 and t not in self._TOUR_NOISE}
        if not mine:
            return []
        out = []
        for r in self.papi_tours["rows"]:
            rn = r.get("tournamentName", r.get("name", "")) or ""
            tid = r.get("tournamentId", r.get("id"))
            if tid is None or bad_league(rn):
                continue
            theirs = {t for t in tokens(rn) if len(t) >= 4 and t not in self._TOUR_NOISE}
            rl = rn.lower()
            if gender == "women" and re.search(r"\bmen\b|\batp\b", rl) and not re.search(r"\bwomen\b|\bwta\b", rl):
                continue                    # a women's event is never joined to a men's tournament (and the reverse)
            if gender == "men" and re.search(r"\bwomen\b|\bwta\b", rl):
                continue
            if mine & theirs:
                out.append((len(mine & theirs) + similarity(n, rn), int(tid)))
        out.sort(reverse=True)
        return [t for _, t in out[:3]]
