"""Pinnacle reference prices for handball, from The Odds API (handball_* sport keys) and OddsPapi (handball tournaments).
Games are joined to SportyBet by TEAM NAMES + KICK-OFF TIME (never by league name), then checked again by the three-way
price agreement (model.agree). Credits are spent only for leagues that really have a SportyBet game to compare."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from handball import cfg, model
from handball.names import pair_score, bad_league, league_key, similarity, tokens
from services.client import get_json, HttpError
from services import odds_api as oa
from services.oddspapi import side_market

log = logging.getLogger("handball.pin")
TIME_TOL_S = 25 * 60
MIN_SCORE = 0.72
PAPI_SPORT = 6


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
                    sel = "home" if name == home else ("away" if name == away else ("draw" if str(name).lower() == "draw" else None))
                    if sel:
                        model.ladder_put(ev, "1x2", None, sel, px)
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


# ------------------------------------------------------------------ OddsPapi handball
_SKIP = ("team total", "corner", "card", "booking", "shot", "offside", "throw", "penalt", "foul", "player", "odd", "even",
         "exact", "range", "first goal", "last goal", "both", "to score", "winning margin", "minute", "interval", "half",
         "clean sheet", "goalscorer", "scorer", "specials", "race", "double", "correct", "highest", "most")
_FT = ("result", "fulltime", "full time", "ft", "match", "game", "regular time", "regulartime")


def classify_fb(cat: dict):
    """OddsPapi catalog entry -> ('1x2' | 'ah' | 'total', handicap) for FULL-TIME markets only, else None."""
    if not cat or cat.get("playerProp"):
        return None
    if str(cat.get("period", "")).lower().replace(" ", "") not in tuple(x.replace(" ", "") for x in _FT):
        return None
    text = ("%s %s" % (cat.get("marketType", ""), cat.get("marketName", ""))).lower()
    outs = cat.get("outcomes") or []
    if "draw no bet" in text or "drawnobet" in text or "dnb" in text:
        return ("ah", 0.0) if len(outs) == 2 else None
    if any(w in text for w in _SKIP):
        return None
    if len(outs) == 3 and any(w in text for w in ("1x2", "moneyline", "match result", "full time result", "3-way", "3way", "result")):
        if "handicap" in text or "spread" in text:
            return None
        return ("1x2", None)
    if len(outs) == 2:
        h = cat.get("handicap")
        if h is None or side_market(cat):
            return None                                   # a team / player market is never a match total or handicap
        if "handicap" in text or "spread" in text:
            return ("ah", float(h))
        if "total" in text or "over" in text or "goals" in text:
            return ("total", float(h))
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
    if nm in ("2", "away", "h2", "team2", "participant2") and kind != "1x2":
        return "away"
    if kind == "1x2":
        if nm in ("x", "draw", "tie"):
            return "draw"
        if nm in ("2", "away", "team2", "participant2"):
            return "away"
        return ("home", "draw", "away")[idx] if idx in (0, 1, 2) else None
    return ("home", "away")[idx] if idx in (0, 1) else None


def normalize_papi(fixtures: list, names: dict, catalog: dict, stats: dict = None) -> list:
    """OddsPapi odds-by-tournaments reply -> pin events (Pinnacle full-time 1X2, Asian handicap, totals)."""
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
            cl = classify_fb(cat)
            if not cl:
                stats.setdefault("skipped", {})[str(cat.get("marketType"))] = 1
                continue
            kind, hcap = cl
            if kind == "total" and not (25.5 <= hcap <= 85.5):
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
        if ev["markets"]:
            out.append(ev)
    return out


def _fix_ah_sign(ev, stats):
    """The handicap line must be the HOME line (a home favourite is negative). If the main line contradicts the 1X2 favourite,
    the feed uses the other convention: flip every AH line of the game (draw-no-bet 0 stays 0)."""
    ah, x = ev["markets"].get("ah"), ev["markets"].get("1x2")
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
        ev["markets"]["ah"] = {round(-ln, 2): d for ln, d in ah.items()}
        stats["ah_flipped"] = stats.get("ah_flipped", 0) + 1


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
            d = json.loads(self._load("hb_pin_spend")) if self._load else {}
        except Exception:
            d = {}
        return d if d.get("day") == self._day() else dict(day=self._day(), oa=0, pp=0)

    def _spent(self, which, n):
        if self.spend.get("day") != self._day():
            self.spend = dict(day=self._day(), oa=0, pp=0)
        self.spend[which] = self.spend.get(which, 0) + n
        try:
            if self._save:
                self._save("hb_pin_spend", json.dumps(self.spend))
        except Exception:
            pass

    def oa_left(self) -> float:
        if self.spend.get("day") != self._day():
            self.spend = dict(day=self._day(), oa=0, pp=0)
        return cfg.f("HB_OA_DAILY_CAP", 600) - self.spend.get("oa", 0)

    def pp_cap(self) -> float:
        cap = cfg.f("HB_PAPI_DAILY_CAP", 120)
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

    # ---- The Odds API: which handball leagues exist / have games (both free calls)
    def handball_keys(self):
        now = self._clock()
        if self.sports["keys"] and now - self.sports["t"] < 12 * 3600:
            return self.sports["keys"]
        data, _ = oa_call(self.oa, "/sports", {})
        keys = [s["key"] for s in data if str(s.get("key", "")).startswith("handball_") and s.get("active", True)
                and not s.get("has_outrights") and not bad_league(s.get("title", "") + " " + s.get("description", ""))]
        # 'handball_epl' etc. are real leagues; '*_winner' keys are outrights
        keys = [k for k in keys if not k.endswith("_winner")]
        self.sports = {"t": now, "keys": keys}
        return keys

    def refresh_index(self, soft_events: list, horizon_s: float):
        """Free: list each handball league's coming games, join them to SportyBet games by names + time."""
        now = self._clock()
        soft = [e for e in soft_events if now - 600 <= e["t"] <= now + horizon_s]
        by_time = sorted(soft, key=lambda e: e["t"])
        keys = self.handball_keys()
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
        # join
        linked = 0
        for se in by_time:
            if se["id"] in self.link:
                continue
            best = None
            for k, ix in self.index.items():
                for (pid, h, a, t) in ix["events"]:
                    if t is None or abs(t - se["t"]) > TIME_TOL_S:
                        continue
                    sc = pair_score(se["home"], se["away"], h or "", a or "")
                    if sc >= MIN_SCORE and (best is None or sc > best[0]):
                        best = (sc, "oa:" + k, pid)
            if best:
                self.link[se["id"]] = (best[1], best[2])
                linked += 1
        self.diag["index"] = dict(leagues=len(keys), listed=n_keys, linked_now=linked, linked_total=len(self.link))
        return linked

    # ---- pulling prices
    def _pull_oa(self, key: str):
        markets = ("h2h", "spreads", "totals")
        if self.oa_left() < len(markets):
            raise oa.OutOfCredits("Odds API daily budget for handball reached")
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
        source, pid = lk
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
                return e, age, ""
        return None, age, "the league was pulled but this game has no Pinnacle price in it"

    # ---- OddsPapi (leagues The Odds API does not carry)
    def _pull_pp(self, source: str):
        tids = [int(x) for x in source[3:].split(",") if x]
        if self.pp_left() < 3:
            raise oa.OutOfCredits("OddsPapi daily budget for handball reached")
        before = self.papi.calls
        try:
            if not self.papi_catalog:
                cat = self.papi.call("/v4/markets", {"language": "en"})
                self.papi_catalog = {int(m["marketId"]): m for m in (cat if isinstance(cat, list) else [])
                                     if m.get("sportId") in (None, self.papi_sport_id()) and str(m.get("marketId", "")).isdigit()}
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

    def papi_sport_id(self) -> int:
        """OddsPapi's id for handball (looked up once from its sport list; 6 when the list cannot be read)."""
        if getattr(self, "_psid", None):
            return self._psid
        sid = PAPI_SPORT
        try:
            rows = self.papi.call("/v4/sports", {"language": "en"}) if self.papi else []
            for r in rows if isinstance(rows, list) else []:
                nm = str(r.get("sportName", r.get("name", ""))).lower()
                if "handball" in nm and "beach" not in nm:
                    sid = int(r.get("sportId", r.get("id", sid)))
                    break
        except Exception as e:
            log.warning("OddsPapi sport list failed: %s", e)
        self._psid = sid
        return sid

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
        for se in soft_events:
            if se["id"] in self.link or self.nolink_until.get(se["id"], 0) > now:
                continue
            tid = self._tour_for(se)
            if tid is None:
                self.nolink_until[se["id"]] = now + 3 * 3600
                continue
            todo.setdefault(tid, []).append(se)
        linked = 0
        for tid, ses in list(todo.items())[:6]:
            src = "pp:%d" % tid
            try:
                if self.age_min(src) is None or self.age_min(src) > 90:
                    self._pull_pp(src)
            except Exception as e:
                log.warning("OddsPapi pull %s failed: %s", src, e)
                for se in ses:
                    self.nolink_until[se["id"]] = now + 3600
                continue
            for se in ses:
                best = None
                for e in self.src[src]["events"]:
                    if e["t"] is None or abs(e["t"] - se["t"]) > TIME_TOL_S:
                        continue
                    sc = pair_score(se["home"], se["away"], e["home"], e["away"])
                    if sc >= MIN_SCORE and (best is None or sc > best[0]):
                        best = (sc, e["id"])
                if best:
                    self.link[se["id"]] = (src, best[1])
                    linked += 1
                else:
                    self.nolink_until[se["id"]] = now + 3600
        return linked

    def _tour_for(self, se):
        """OddsPapi tournament id whose country AND name match the SportyBet league, or None (never a guess)."""
        c, n = " ".join(tokens(se.get("country", ""))), se.get("tname", "")
        if bad_league(n):
            return None
        scored = []
        for r in self.papi_tours["rows"]:
            rc = " ".join(tokens(r.get("categoryName", r.get("category", "")) or ""))
            rn = r.get("tournamentName", r.get("name", "")) or ""
            if rc != c or bad_league(rn):
                continue
            tid = r.get("tournamentId", r.get("id"))
            if tid is None:
                continue
            scored.append((similarity(n, rn), int(tid)))
        scored.sort(reverse=True)
        if not scored or scored[0][0] < 0.85:
            return None
        if len(scored) > 1 and scored[1][0] > scored[0][0] - 0.08:
            return None
        return scored[0][1]
