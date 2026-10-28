"""OddsPapi client: Pinnacle prices for leagues The Odds API does not carry (second Pinnacle source).

Endpoints (https://api.oddspapi.io, apiKey query param): /v4/markets (catalog), /v4/participants?sportId=11
(id -> team name) and /v4/odds-by-tournaments?bookmaker=pinnacle&tournamentIds=... (one call can cover several
leagues). Free keys allow ~250 requests/month each and a 1 s endpoint cooldown, so calls are spread round-robin over
all keys, each key is used at most every SPACING seconds, and exhausted keys are retired for the session.
normalize() returns the SAME event shape as services.odds_api.normalize_event, so core.pinref uses it unchanged."""
import os
import re
import time
from datetime import datetime, timezone

from services.client import get_json, HttpError
from services.logger import get_logger

log = get_logger("oddspapi")
BASE = "https://api.oddspapi.io"
BASE5 = "https://v5.oddspapi.io/en"   # newer API generation (fixtures/live, fixtures/odds, fixtures/settlement)
BASKETBALL = 11
BOOK = "pinnacle"
# real OddsPapi catalog (probe 2026-10-11): full game incl. overtime = period "result"; halves are "p1+p2", quarters "p1".."p4"
FULL_TIME = ("result", "fulltime", "full time", "ft", "match", "game")
_SKIP_WORDS = ("team total", "odd", "even", "player", "race", "margin", "exact", "range", "first", "last", "both",
               "double", "to score", "winning")


def keys_from_env():
    raw = (os.environ.get("ODDSPAPI_KEYS") or os.environ.get("ODDSPAPI_KEY") or "") + "," + os.environ.get("ODDSPAPI_KEYS_EXTRA", "")
    out = []
    for k in raw.replace(";", ",").replace("\n", ",").split(","):
        k = k.strip()
        if k and k not in out:            # ODDSPAPI_KEYS_EXTRA adds keys to this service without touching its main list
            out.append(k)
    return out


class NoKeys(Exception):
    pass


def classify(mkt: dict):
    """Catalog entry -> ('h2h'|'spreads'|'totals', period_is_full, handicap) or None."""
    if mkt.get("playerProp"):
        return None
    t = str(mkt.get("marketType", "")).lower()
    n = str(mkt.get("marketName", "")).lower()
    text = (t + " " + n).replace("overtime", "").replace("over time", "")  # "incl. overtime" is not "over"
    for w in _PERIOD_WORDS:      # "first quarter" must not trip the "first basket" skip word
        text = text.replace(w, "")
    if any(w in text for w in _SKIP_WORDS):
        return None
    outs = mkt.get("outcomes") or []
    if len(outs) != 2:
        return None
    if "spread" in text or "handicap" in text:
        kind = "spreads"
    elif "total" in text or "over" in text:
        kind = "totals"
    elif "moneyline" in text or "money line" in text or t in ("12", "h2h", "winner") or "to win" in text:
        kind = "h2h"
    else:
        return None
    per = str(mkt.get("period", "")).lower().strip()
    return kind, per in FULL_TIME, mkt.get("handicap")


_PERIOD_WORDS = ("first quarter", "second quarter", "third quarter", "fourth quarter", "last quarter",
                 "first half", "second half", "1st half", "2nd half")


def period_code(mkt: dict, n_periods: int = 4):
    """'full' | 'q1'..'q4' | 'h1' | None. Real catalog (2026-10-11): full game = 'result', quarters 'p1'..'p4',
    first half 'p1+p2'. Quarters are only read for 4-period leagues (a 2-period league's 'p1' is a half)."""
    per = str(mkt.get("period", "")).lower().strip().replace(" ", "")
    if per in FULL_TIME:
        return "full"
    if per == "p1+p2":
        return "h1"
    # second half and Q4 only exist as plain totals; their handicaps (and the 'overtime' variants) include overtime
    if n_periods == 4 and per == "p3+p4":
        return "h2"
    if n_periods == 4 and per == "p4+overtime":
        return "q4ot"
    if n_periods == 4 and per == "p3+p4+overtime":
        return "h2ot"
    if n_periods == 4 and per in ("p1", "p2", "p3", "p4"):
        return "q" + per[1]
    return None


def _side(kind, outcome_name, idx):
    nm = str(outcome_name).lower().strip()
    if kind == "totals":
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


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def normalize(fixtures: list, names: dict, catalog: dict, league: str, stats: dict = None) -> list:
    """fixtures: odds-by-tournaments reply; names: {participantId(str): name}; catalog: {marketId(int): market}.
    Only full-game moneyline / spread / total of Pinnacle are returned. stats (optional dict) collects what else was
    seen, e.g. period codes, so a probe can say whether quarter markets exist."""
    stats = stats if stats is not None else {}
    try:
        from core.live_model import fmt
        n_periods = fmt(league)[0]
    except Exception:
        n_periods = 4
    events = []
    for fx in fixtures or []:
        bo = ((fx.get("bookmakerOdds") or {}).get(BOOK) or {})
        home = names.get(str(fx.get("participant1Id"))) or fx.get("participant1Name")
        away = names.get(str(fx.get("participant2Id"))) or fx.get("participant2Name")
        if not home or not away:
            stats["no_names"] = stats.get("no_names", 0) + 1
            continue
        ev = dict(event_id="papi-%s" % fx.get("fixtureId"), league=league, home=home, away=away,
                  commence=_dt(fx.get("startTime")), markets={})
        for mid, m in (bo.get("markets") or {}).items():
            try:
                cat = catalog.get(int(mid))
            except (TypeError, ValueError):
                cat = None
            if not cat:
                stats.setdefault("unknown_market_ids", set()).add(str(mid))
                continue
            key = (str(cat.get("marketType")), str(cat.get("period")))
            stats.setdefault("seen", {})[key] = stats.setdefault("seen", {}).get(key, 0) + 1
            cl = classify(cat)
            if not cl:
                continue
            code = period_code(cat, n_periods)
            kind, _, hcap = cl
            if code is None or (code != "full" and kind == "h2h"):
                continue       # quarter / half winners are three-way on SportyBet: only handicaps and totals
            if code in ("q4", "h2") and kind == "spreads":
                continue       # a regulation-only Q4 / 2nd-half handicap does not exist at Pinnacle: never guess one
            if code in ("q4ot", "h2ot") and kind != "spreads":
                continue       # the overtime variants are only used for handicaps (their totals would clash with p4 / p3+p4)
            mkey = kind if code == "full" else "%s_%s" % (kind, code)
            if kind != "h2h" and hcap is None:
                continue
            order = [o.get("outcomeId") for o in cat.get("outcomes", [])]
            names_by_id = {o.get("outcomeId"): o.get("outcomeName") for o in cat.get("outcomes", [])}
            for oid, o in (m.get("outcomes") or {}).items():
                pl = (o.get("players") or {}).get("0") or next(iter((o.get("players") or {}).values()), None)
                if not pl or pl.get("active") is False:
                    continue
                try:
                    price = float(pl.get("price"))
                except (TypeError, ValueError):
                    continue
                if price <= 1:
                    continue
                try:
                    oid_i = int(oid)
                except (TypeError, ValueError):
                    oid_i = oid
                idx = order.index(oid_i) if oid_i in order else -1
                sel = _side(kind, names_by_id.get(oid_i, ""), idx)
                if not sel:
                    continue
                line = None if kind == "h2h" else float(hcap)
                if kind == "totals" and code == "full":
                    from core.tracker import total_range
                    lo, hi = total_range(league)
                    if not (lo <= line <= hi):
                        stats["odd_total"] = stats.get("odd_total", 0) + 1
                        continue       # a half / team line filed under the full game: never used as the full-game total
                ev["markets"].setdefault(mkey, []).append(dict(book=BOOK, selection=sel, line=line, odds=price))
        _fix_spread_sign(ev, stats)
        if ev["markets"]:
            events.append(ev)
    return events


def _main_gap_line(outs, kinds):
    """Line (of a spread ladder) whose two prices are closest to even."""
    by = {}
    for o in outs:
        by.setdefault(o["line"], {})[o["selection"]] = o["odds"]
    best = None
    for ln, s in by.items():
        if kinds[0] in s and kinds[1] in s:
            gap = abs(s[kinds[0]] - s[kinds[1]])
            if best is None or gap < best[0]:
                best = (gap, ln)
    return best[1] if best else None


def _fix_spread_sign(ev, stats):
    """The spread line must be the HOME line (home favourite = negative). If the feed's convention is the other way
    round, the main line contradicts the moneyline favourite: flip every spread line of the game (quarter and half
    ladders too). Games with a near pick'em line (|line| < 1.5) are left as they are."""
    sp = ev["markets"].get("spreads")
    ml = ev["markets"].get("h2h")
    if not sp or not ml:
        return
    ln = _main_gap_line(sp, ("home", "away"))
    prices = {o["selection"]: o["odds"] for o in ml}
    if ln is None or abs(ln) < 1.5 or "home" not in prices or "away" not in prices:
        return
    home_fav = prices["home"] < prices["away"]
    if (home_fav and ln > 0) or (not home_fav and ln < 0):
        for key, outs in ev["markets"].items():
            if key.startswith("spreads"):
                for o in outs:
                    o["line"] = -o["line"]
        stats["spread_flipped"] = stats.get("spread_flipped", 0) + 1
    else:
        stats["spread_ok"] = stats.get("spread_ok", 0) + 1


_QC = {}


def side_market(cat: dict) -> bool:
    """True for a market about ONE side (team total, player total, home/away spread ...). It must never be read as a whole-match total or
    handicap: its lines overlap the real ones (a team total of 1.5 goals looks like a match total of 1.5)."""
    t = re.sub(r"[^a-z0-9]", "", str((cat or {}).get("marketType", "")).lower())
    n = " " + re.sub(r"[^a-z0-9 ]", " ", str((cat or {}).get("marketName", "")).lower()) + " "
    return (any(w in t for w in ("teamtotal", "playertotal", "teamspread", "team1", "team2", "participant1", "participant2", "hometotal", "awaytotal"))
            or any(w in n for w in (" team ", " player ", " home ", " away ", " participant ")))


_DONE = {"WIN", "LOSE", "PUSH", "HALFWIN", "HALFLOSS"}
_ENDED = ("end", "finish", "closed", "complete", "final")


def settlement_summary(data):
    """/fixtures/settlement reply -> dict(state 'finished' | 'void', rows {period: (score1, score2)}) or None while the game is not over
    or the reply is not shaped as documented (the pick then stays open for /result: nothing is ever graded from a guess)."""
    if not isinstance(data, dict):
        return None
    sts = [str(x.get("status") or "").upper() for x in data.get("settlements") or [] if isinstance(x, dict)]
    rows = {}
    for k, v in (data.get("scores") or {}).items():
        try:
            rows[str(k)] = (int(v["participant1Score"]), int(v["participant2Score"]))
        except (KeyError, TypeError, ValueError):
            continue
    name = str(((data.get("status") or {}) if isinstance(data.get("status"), dict) else {}).get("statusName") or "").lower()
    if sts and all(x == "CANCELLED" for x in sts):
        return dict(state="void", rows=rows)
    if name and not any(w in name for w in _ENDED):
        return None                                   # still being played, postponed or similar: do not grade
    if any(x in _DONE for x in sts) and rows:
        return dict(state="finished", rows=rows)
    return None


def quota_client():
    """One shared, never-metered client used only for the account probe (keys read from the environment)."""
    if "c" not in _QC:
        _QC["c"] = OddsPapi()
    return _QC["c"]


class OddsPapi:
    def __init__(self, keys=None, spacing=None, get=None, sleep=None, clock=None):
        self.keys = list(keys if keys is not None else keys_from_env())
        self.spacing = float(os.environ.get("ODDSPAPI_SPACING_SEC", 6)) if spacing is None else spacing
        self._get = get or get_json
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time
        self.exhausted = set()
        self.last_used = {}
        self.calls = 0
        self.quota_t = -1e12          # last /v4/account probe
        self.quota_rows = None        # [(limit, used)] per key, or None when the account reply was not understood
        self.quota_raw = None         # field names seen in the reply (logged once, shown in /status)

    def _pick(self):
        live = [k for k in self.keys if k not in self.exhausted]
        if not live:
            raise NoKeys("no OddsPapi key left")
        return min(live, key=lambda k: self.last_used.get(k, 0))

    def call(self, path, params=None, base=None):
        base = base or BASE
        params = dict(params or {})
        tries = 0
        while True:
            key = self._pick()
            wait = self.spacing - (self._clock() - self.last_used.get(key, 0))
            if wait > 0:
                self._sleep(wait)
            self.last_used[key] = self._clock()
            try:
                data, _ = self._get(base + path, dict(params, apiKey=key))
                self.calls += 1
                return data
            except HttpError as e:
                detail = str(e).replace(key, "***")[:160]
                tries += 1
                if e.status in (401, 402, 403) or "quota" in str(e).lower():
                    log.warning("OddsPapi key ...%s retired (%s): %s", key[-4:], e.status, detail)
                    self.exhausted.add(key)
                    continue
                if e.status == 429 and tries < 4:
                    log.warning("OddsPapi 429 on key ...%s, retry %d", key[-4:], tries)
                    self._sleep(self.spacing)
                    continue
                raise HttpError(e.status, detail)

    # --- monthly quota (free keys: ~250 requests a month each, reset on the 1st) ---
    _LIMIT_F = ("request_limit", "requestLimit", "requests_limit", "limit", "quota")
    _USED_F = ("requests_used", "request_used", "requestsUsed", "request_count", "requests", "used", "usage")
    _LEFT_F = ("requests_remaining", "request_remaining", "requestsRemaining", "remaining", "requests_left")

    @classmethod
    def parse_account(cls, data):
        """-> (limit, used) from a /v4/account reply, or None when the field names are not recognised."""
        if not isinstance(data, dict):
            return None
        subs = data.get("subscriptions")
        if isinstance(subs, list) and subs:           # documented shape: request_limit / request_count sit inside the subscription
            cur = data.get("current_subscription_id")
            sub = next((x for x in subs if isinstance(x, dict) and x.get("subscription_id") == cur), None) \
                or next((x for x in subs if isinstance(x, dict) and x.get("is_active")), None) or subs[0]
            lim, cnt = (sub or {}).get("request_limit"), (sub or {}).get("request_count")
            if isinstance(lim, (int, float)) and isinstance(cnt, (int, float)):
                return float(lim), float(cnt)
        flat = dict(data)
        for v in list(data.values()):
            if isinstance(v, dict):
                flat.update({k: x for k, x in v.items() if k not in flat})
        num = lambda names: next((float(flat[n]) for n in names if isinstance(flat.get(n), (int, float)) and not isinstance(flat.get(n), bool)), None)
        lim, used, left = num(cls._LIMIT_F), num(cls._USED_F), num(cls._LEFT_F)
        if lim is None:
            return None
        if used is None and left is not None:
            used = lim - left
        return (lim, used) if used is not None else None

    def probe_quota(self, max_age=3 * 3600):
        """Ask every key for its real monthly limit and use (/v4/account is not metered). Cached; never raises."""
        now = self._clock()
        if now - self.quota_t < max_age:
            return self.quota_rows
        self.quota_t = now
        rows, seen = [], None
        for key in self.keys:
            try:
                data, _ = self._get(BASE + "/v4/account", {"apiKey": key})
            except Exception as e:
                log.warning("OddsPapi account probe failed for key ...%s: %s", key[-4:], str(e).replace(key, "***")[:80])
                continue
            seen = sorted(data) if isinstance(data, dict) else type(data).__name__
            r = self.parse_account(data)
            if r:
                rows.append(r)
        if seen is not None and self.quota_raw is None:
            self.quota_raw = seen
            log.info("OddsPapi /v4/account fields: %s", seen)
        self.quota_rows = rows if rows and len(rows) == len(self.keys) else None
        return self.quota_rows

    def probe_quota_async(self, max_age=3 * 3600):
        """Start the account probe in a background thread when it is stale; return what is known now (never blocks)."""
        import threading
        if self._clock() - self.quota_t >= max_age and not getattr(self, "_probing", False):
            self._probing = True

            def run():
                try:
                    self.probe_quota(max_age)
                finally:
                    self._probing = False
            threading.Thread(target=run, daemon=True).start()
        return self.quota_rows

    def daily_allowance(self, spent_today: float = 0.0) -> float:
        """Calls this service may spend today without running its keys dry before the month ends."""
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        import calendar
        dim = calendar.monthrange(now.year, now.month)[1]
        days_left = max(1, dim - now.day + 1)
        burn = float(os.environ.get("ODDSPAPI_BURN", 1.0))      # >1 spends the month's quota a little faster (e.g. 1.3 in the last week)
        rows = self.probe_quota()
        if rows:
            return max(0.0, burn * (sum(l - u for l, u in rows) + spent_today) / days_left)   # account figure already excludes today's calls
        per_key = float(os.environ.get("ODDSPAPI_KEY_MONTHLY", 250))
        live = len([k for k in self.keys if k not in self.exhausted])
        return burn * live * per_key / dim

    # --- data ---
    def settlement(self, fixture_id) -> dict:
        """One request: final scores and the result of every market outcome of a finished fixture (v5 API)."""
        return self.call("/fixtures/settlement", {"fixtureId": str(fixture_id)}, base=BASE5)

    def catalog(self) -> dict:
        data = self.call("/v4/markets", {"language": "en"})
        out = {}
        for m in data if isinstance(data, list) else []:
            if m.get("sportId") in (None, BASKETBALL):
                try:
                    out[int(m["marketId"])] = m
                except (KeyError, TypeError, ValueError):
                    pass
        return out

    def participants(self) -> dict:
        data = self.call("/v4/participants", {"sportId": BASKETBALL, "language": "en"})
        return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}

    def tournaments(self) -> list:
        """Basketball tournaments OddsPapi knows (id, name, country): lets /tournaments find an id by name."""
        data = self.call("/v4/tournaments", {"sportId": BASKETBALL, "language": "en"})
        return data if isinstance(data, list) else []

    def odds(self, tournament_ids) -> list:
        data = self.call("/v4/odds-by-tournaments", {"bookmaker": BOOK,
                                                     "tournamentIds": ",".join(str(t) for t in tournament_ids)})
        return data if isinstance(data, list) else []
