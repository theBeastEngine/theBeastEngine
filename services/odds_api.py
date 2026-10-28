"""The Odds API client. Cost per call = markets x regions (10 bookmakers = 1 region)."""
import time
from datetime import datetime, timezone
from config.settings import settings
from services.client import get_json, HttpError
from services.logger import get_logger

log = get_logger("odds_api")
BASE = "https://api.the-odds-api.com/v4"
MAIN_MARKETS = ("h2h", "spreads", "totals")
MAX_429_RETRIES = 3


class OutOfCredits(Exception):
    pass


class KeyBook:
    """Shared memory of the Odds API keys: which are out of credits (and until when) and the credits each reported last.
    Kept in the engine's own database (never the key itself, only a short hash), so a restart does not forget a dead key.
    A dead key goes to the END of the order and is tried again after DEAD_HOURS (24 h; the allowance refills each month)."""

    DEAD_HOURS = 24.0
    RATE_HOURS = 2.0

    def __init__(self):
        self.load = self.save = None
        self._d = None

    @staticmethod
    def h(key) -> str:
        import hashlib
        return hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:10]

    def _s(self) -> dict:
        if self._d is None:
            import json
            try:
                blob = self.load() if self.load else None
                self._d = json.loads(blob) if blob else {}
            except Exception:
                self._d = {}
            self._d.setdefault("dead", {})
            self._d.setdefault("rem", {})
        return self._d

    def _flush(self):
        try:
            import json
            if self.save and self._d is not None:
                self.save(json.dumps(self._d))
        except Exception as e:
            log.warning("key state not saved: %s", e)

    def is_dead(self, key, now=None) -> bool:
        return self._s()["dead"].get(self.h(key), 0) > (now or time.time())

    def mark_dead(self, key, hours=None, now=None):
        now = now or time.time()
        self._s()["dead"][self.h(key)] = now + 3600 * (self.DEAD_HOURS if hours is None else hours)
        self._flush()

    def note_remaining(self, key, rem, now=None):
        try:
            self._s()["rem"][self.h(key)] = [float(rem), int(now or time.time())]
        except (TypeError, ValueError):
            return
        self._flush()

    def rows(self, keys, now=None):
        """[(last4, credits left or None, dead_until epoch or None)] in the order the keys are tried."""
        now = now or time.time()
        s = self._s()
        out = []
        for k in keys:
            hk = self.h(k)
            until = s["dead"].get(hk, 0)
            rem = (s["rem"].get(hk) or [None])[0]
            out.append((k[-4:], rem, until if until > now else None))
        return sorted(out, key=lambda r: 1 if r[2] else 0)

    def summary(self, keys, now=None):
        r = self.rows(keys, now)
        return len(r), sum(1 for x in r if x[2] is None), sum(1 for x in r if x[2] is not None)


_book = KeyBook()


def book() -> KeyBook:
    return _book


def _dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def normalize_event(raw: dict, league: str) -> dict:
    """-> {event_id, league, home, away, commence, markets: {market: [{book, selection, line, odds}]}}
    Spread line is always the HOME line (away outcome's point is negated)."""
    home, away = raw["home_team"], raw["away_team"]
    ev = dict(event_id=raw["id"], league=league, home=home, away=away,
              commence=_dt(raw.get("commence_time")), markets={})
    for b in raw.get("bookmakers", []):
        for m in b.get("markets", []):
            key = m["key"]
            if key not in MAIN_MARKETS:
                continue
            for o in m.get("outcomes", []):
                name, pt = o.get("name"), o.get("point")
                if name == home:
                    sel, line = "home", pt
                elif name == away:
                    sel, line = "away", (-pt if (key == "spreads" and pt is not None) else pt)
                elif str(name).lower() in ("over", "under"):
                    sel, line = str(name).lower(), pt
                else:
                    continue
                if key == "h2h":
                    line = None
                ev["markets"].setdefault(key, []).append(
                    dict(book=b["key"], selection=sel, line=line, odds=float(o["price"])))
    return ev


class OddsAPI:
    def __init__(self, keys=None):
        self.keys = list(keys or settings.ODDS_API_KEYS)
        self.exhausted = set()
        self.remaining = {}

    def masked_remaining(self) -> dict:
        """CLAUDE FIX: safe to print or log (full keys never leave this object)."""
        return {f"...{k[-4:]}": v for k, v in self.remaining.items()}

    def _key(self):
        bk = book()
        now = time.time()
        self.exhausted = {k for k in self.exhausted if bk.is_dead(k, now)}      # a key whose rest day is over is tried again
        for k in self.keys:                                                     # working keys first, in your order
            if k not in self.exhausted and not bk.is_dead(k, now):
                return k
        raise OutOfCredits("no Odds API key with credits left")

    def _dead(self, key, status):
        self.exhausted.add(key)
        book().mark_dead(key, KeyBook.RATE_HOURS if status == 429 else KeyBook.DEAD_HOURS)

    def fetch(self, sport_key: str, markets=MAIN_MARKETS, books=None, regions="eu") -> list:
        retries = 0
        while True:
            key = self._key()
            params = {"apiKey": key, "markets": ",".join(markets), "oddsFormat": "decimal"}
            if books:
                params["bookmakers"] = ",".join(books)
            else:
                params["regions"] = regions
            try:
                data, hdr = get_json(f"{BASE}/sports/{sport_key}/odds", params)
            except HttpError as e:
                detail = str(e).replace(key, "***")[:200]
                # CLAUDE FIX: 429 is a rate limit (burst), not an empty quota: wait and retry instead of killing the key
                if e.status == 429 and retries < MAX_429_RETRIES:
                    retries += 1
                    log.warning("rate limited (429), retry %s/%s", retries, MAX_429_RETRIES)
                    time.sleep(2 * retries)
                    continue
                if e.status in (401, 429):
                    log.warning("key ...%s exhausted/invalid (%s): %s", key[-4:], e.status, detail)
                    self._dead(key, e.status)
                    continue
                raise
            used = int(float(hdr.get("x-requests-last", len(markets))))
            rem = hdr.get("x-requests-remaining")
            self.remaining[key] = rem
            try:  # CLAUDE FIX: retire a key as soon as it reports 0 left, before the next call fails
                if rem is not None:
                    book().note_remaining(key, rem)
                if rem is not None and float(rem) <= 0:
                    self._dead(key, 401)
            except (TypeError, ValueError):
                pass
            try:
                from storage.repository import log_api_usage
                log_api_usage(key, f"odds/{sport_key}", used)
            except Exception as e:  # never let bookkeeping break a fetch
                log.warning("usage log failed: %s", e)
            return data

    def scores(self, sport_key: str, days_from: int = 1) -> list:
        """Finished (and live) games of the last days_from days (1-3) with their final score. Costs 2 credits."""
        retries = 0
        while True:
            key = self._key()
            try:
                data, hdr = get_json(f"{BASE}/sports/{sport_key}/scores", {"apiKey": key, "daysFrom": max(1, min(3, int(days_from)))})
            except HttpError as e:
                detail = str(e).replace(key, "***")[:200]
                if e.status == 429 and retries < MAX_429_RETRIES:
                    retries += 1
                    time.sleep(2 * retries)
                    continue
                if e.status in (401, 429):
                    log.warning("key ...%s exhausted/invalid (%s): %s", key[-4:], e.status, detail)
                    self._dead(key, e.status)
                    continue
                raise
            rem = hdr.get("x-requests-remaining")
            self.remaining[key] = rem
            try:
                if rem is not None:
                    book().note_remaining(key, rem)
                if rem is not None and float(rem) <= 0:
                    self._dead(key, 401)
            except (TypeError, ValueError):
                pass
            return data
