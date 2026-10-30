"""Per-game Pinnacle reference for prematch.

Pinnacle's real price for the SAME game is the fair-price reference; SportyBet (soft book) prices are compared
against it. One Odds API call per league (3 credits: h2h + spreads + totals, Pinnacle only) is cached on disk and
reused until it is older than PIN_REF_MAX_AGE_MIN. Credits are tracked per UTC day and capped (PIN_REF_DAILY_CAP).
Pinnacle data older than PIN_REF_STALE_MAX_MIN, or for a game that has already started, is never used."""
import difflib
import json
import os
import re
import tempfile
import unicodedata
import threading
from datetime import datetime, timedelta, timezone

from config.leagues import LEAGUES, pin_source
from config.settings import settings, PINNACLE, MAX_EV_SANITY
from core import devig as _devig
from core.ev_engine import edge
from services.odds_api import OddsAPI, normalize_event, MAIN_MARKETS
from services.logger import get_logger

log = get_logger("pinref")

LIVE_KEEP_HOURS = 5  # in-play games stay in the cache this long after tip-off
PAPI_EMPTY_RETRY_MIN = 45  # OddsPapi league with fixtures but no price: look again after this long
IDLE_HORIZON_DAYS = 4   # a league counts as ACTIVE when a Pinnacle game starts within this many days (3-day window + 1)
COST_PER_REFRESH = len(MAIN_MARKETS)  # markets x 1 region (Pinnacle only)
_TWO_WAY = {"h2h": ("home", "away"), "spreads": ("home", "away"), "totals": ("over", "under")}
for _p in ("q1", "q2", "q3", "q4", "h1", "h2", "q4ot", "h2ot"):   # quarter / first-half ladders (OddsPapi); same two-way sides
    _TWO_WAY["spreads_" + _p] = ("home", "away")
    _TWO_WAY["totals_" + _p] = ("over", "under")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(dt):
    """datetime / ISO string / epoch (s or ms) -> naive UTC datetime, or None."""
    if dt is None or dt == "":
        return None
    if isinstance(dt, (int, float)):
        return datetime.fromtimestamp(dt / 1000 if dt > 1e11 else dt, timezone.utc).replace(tzinfo=None)
    if isinstance(dt, str):
        s = dt.strip()
        if s.isdigit():
            return _naive(int(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse(s):
    if not s:
        return None
    try:
        return _naive(datetime.fromisoformat(s))
    except ValueError:
        return None


def _ln(line):
    return None if line is None else round(float(line), 3)


def _n(s):
    # CLAUDE FIX: fold accents first (Munchen, not Mnchen: the old regex deleted the u-umlaut)
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", "", s).strip()


# club-type words and spellings that differ between feeds (SportyBet vs Pinnacle)
_NOISE = {"fc", "kk", "bc", "bk", "cb", "sc", "ssd", "sa",
          # sponsor / place decorations that only ONE feed adds ("Saski Baskonia" / "Baskonia Vitoria-Gasteiz")
          "saski", "vitoria", "gasteiz", "vitoriagasteiz"}
_ALIAS = {"munchen": "munich", "olympiakos": "olympiacos"}


def _words(s):
    return [_ALIAS.get(t, t) for t in _n(s).split() if t not in _NOISE]


def _tokens(s):
    return set(_words(s))


def _different_heads(a, b):
    """True when the first words differ and neither appears in the other name: Paris Basketball / Dubai Basketball,
    Maccabi Tel Aviv / Hapoel Tel Aviv, Partizan Belgrade / Crvena Zvezda Belgrade are different clubs even though
    their LAST words are equal (a derby would otherwise match with home and away swapped)."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    ha, hb = wa[0], wb[0]
    return ha != hb and len(ha) >= 4 and len(hb) >= 4 and ha not in wb and hb not in wa


def _compact(s):
    return _n(s).replace(" ", "")


def _same(a, b):
    """Team-name match across feeds: same letters, or near-identical, or same nickname (last word)."""
    x, y = _compact(a), _compact(b)
    if not x or not y:
        return False
    if x == y:
        return True
    if difflib.SequenceMatcher(None, x, y).ratio() >= 0.86:
        return True
    xt, yt = _n(a).split(), _n(b).split()
    if len(xt[-1]) >= 4 and xt[-1] == yt[-1] and not _different_heads(a, b):
        return True
    # CLAUDE FIX: one name contains all the words of the other ("KK Crvena zvezda Belgrade" / "KK Crvena zvezda").
    # Both teams of a game must match and the start times must agree, so this stays safe.
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb:
        small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if small <= big and (len(small) >= 2 or len(next(iter(small))) >= 7):
            return True
    return False


def _two_way(a, b):
    """Fair probabilities of a two-way market (margin removed). Uses core.devig, falls back to proportional."""
    try:
        r = _devig.devig([a, b])
        pa, pb = float(r[0]), float(r[1])
        if abs(pa + pb - 1) < 1e-6 and 0 < pa < 1 and 0 < pb < 1:
            return pa, pb
    except Exception:
        pass
    ia, ib = 1 / a, 1 / b
    s = ia + ib
    return ia / s, ib / s


def fair_probs(pin_ev) -> dict:
    """{(market, line): {selection: fair_prob}} from a Pinnacle event (only complete two-way pairs)."""
    groups = {}
    for m, outs in pin_ev.get("markets", {}).items():
        if m not in _TWO_WAY:
            continue
        for o in outs:
            if o.get("book") == PINNACLE:
                groups.setdefault((m, _ln(o.get("line"))), {})[o["selection"]] = o["odds"]
    fair = {}
    for (m, ln), sides in groups.items():
        a_s, b_s = _TWO_WAY[m]
        if a_s in sides and b_s in sides and sides[a_s] > 1 and sides[b_s] > 1:
            pa, pb = _two_way(sides[a_s], sides[b_s])
            fair[(m, ln)] = {a_s: pa, b_s: pb}
    return fair


def _default_path():
    """Keep the cache next to the database (Render's persistent disk), so a redeploy does not re-spend credits."""
    explicit = os.environ.get("PIN_REF_PATH")
    if explicit:
        return explicit
    db = os.environ.get("DB_PATH") or getattr(settings, "DB_PATH", None)
    return os.path.join(os.path.dirname(db) or ".", "pin_ref.json") if db else "data/pin_ref.json"


def _remote_kv():
    """(get, set) functions backed by the database when Neon is active, else None."""
    try:
        from storage import db, repository
        if db.is_remote():
            return repository.kv_get, repository.kv_set
    except Exception as e:
        log.warning("pinref: database cache unavailable (%s); using the file cache", e)
    return None


class PinRef:
    on_refresh = None   # optional callback(league, events, scope): the engine logs schedules and price history

    def __init__(self, api=None, path=None, max_age_min=None, stale_max_min=None, daily_cap=None):
        self._api = api
        self.path = path or _default_path()
        # Neon: with no explicit path the cache and credit counter live in the database, so a deploy does not
        # re-spend credits. An explicit path (tests, PIN_REF_PATH) keeps the old file behaviour.
        self._kv = _remote_kv() if path is None and not os.environ.get("PIN_REF_PATH") else None
        self._lock = threading.RLock()  # scheduler thread + /beast command must not both spend credits
        self.max_age_min = float(os.environ.get("PIN_REF_MAX_AGE_MIN", 240)) if max_age_min is None else max_age_min
        self.stale_max_min = float(os.environ.get("PIN_REF_STALE_MAX_MIN", 360)) if stale_max_min is None else stale_max_min
        self.daily_cap = int(os.environ.get("PIN_REF_DAILY_CAP", 30)) if daily_cap is None else daily_cap
        # CLAUDE FIX: a league with no game close to tip-off (NCAAB out of season, NBA 11 days before opening night)
        # was refetched every max_age_min for nothing. Idle leagues are refreshed at most this often (minutes);
        # PIN_REF_IDLE_MAX_AGE_MIN=240 gives the old behaviour back.
        self.idle_max_age_min = float(os.environ.get("PIN_REF_IDLE_MAX_AGE_MIN", 1440))
        # OddsPapi (second Pinnacle source): calls per UTC day are capped separately from Odds API credits
        self.papi_daily_cap = int(os.environ.get("PAPI_DAILY_CAP", 80))
        self._papi = None
        self._papi_names = {}
        self._papi_names_at = 0.0
        self._live_snap = {}     # league -> (monotonic-ish time, normalized current Pinnacle events); see live_fetch
        self._live_raw = {}
        self._live_diag = {}   # league -> {source: what the last live fetch saw}, shown by /livepin
        self.data = self._load()

    # --- storage ---
    def _load(self):
        try:
            if self._kv:
                raw = self._kv[0]("pin_ref")
                d = json.loads(raw) if raw else {}
            else:
                with open(self.path, encoding="utf-8") as f:
                    d = json.load(f)
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        d.setdefault("leagues", {})
        d.setdefault("spend", {})
        return d

    def _save(self):
        if self._kv:
            try:
                self._kv[1]("pin_ref", json.dumps(self.data))
            except Exception as e:
                log.warning("pinref save to database failed: %s", e)
            return
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder or ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("pinref save failed: %s", e)

    # --- credits ---
    def spent_today(self) -> int:
        sp = self.data["spend"]
        return int(sp.get("credits", 0)) if sp.get("day") == _now().date().isoformat() else 0

    def _add_spend(self, n):
        today = _now().date().isoformat()
        self.data["spend"] = {"day": today, "credits": self.spent_today() + n}

    # --- fetch ---
    def _age_min(self, blob):
        t = _parse(blob.get("fetched_at"))
        return float("inf") if t is None else (_now() - t).total_seconds() / 60

    def refresh(self, league: str) -> int:
        with self._lock:
            return self._refresh(league)

    def _papi_spent_today(self) -> int:
        sp = self.data.get("papi_spend") or {}
        return int(sp.get("calls", 0)) if sp.get("day") == _now().date().isoformat() else 0

    def _papi_catalog(self, client):
        cat = self.data.get("papi_catalog") or {}
        t = _parse(cat.get("at"))
        if cat.get("markets") and t and (_now() - t).total_seconds() < 7 * 86400:
            return {int(k): v for k, v in cat["markets"].items()}
        full = client.catalog()
        keep = {str(k): {f: v.get(f) for f in ("marketId", "marketName", "marketType", "period", "handicap",
                                               "outcomes", "playerProp")} for k, v in full.items()}
        self.data["papi_catalog"] = {"at": _now().isoformat(), "markets": keep}
        return {int(k): v for k, v in keep.items()}

    def _papi_fetch(self, tids, league):
        """OddsPapi fetch for `tids` (1-3 calls: odds, plus the market catalog / team names when missing or old).
        Returns (raw fixtures, normalized events, stats). Spend is counted even when a call fails."""
        import time as _t
        from services import oddspapi as op
        if self._papi is None:
            self._papi = op.OddsPapi()
        client = self._papi
        before = client.calls
        try:
            catalog = self._papi_catalog(client)
            raw = client.odds(list(tids))
            ids = {str(f.get(k)) for f in raw for k in ("participant1Id", "participant2Id")}
            if (ids - set(self._papi_names)) and _t.time() - self._papi_names_at > 3600:
                self._papi_names = client.participants()
                self._papi_names_at = _t.time()
            stats = {}
            normalized = op.normalize(raw, self._papi_names, catalog, league, stats)
        finally:
            self.data["papi_spend"] = {"day": _now().date().isoformat(),
                                       "calls": self._papi_spent_today() + (client.calls - before)}
        return raw, normalized, stats

    # --- live check: CURRENT Pinnacle prices of games in play (OddsPapi only; see core/livepin.py) ---
    def _live_spent_today(self) -> int:
        sp = self.data.get("live_spend") or {}
        return int(sp.get("calls", 0)) if sp.get("day") == _now().date().isoformat() else 0

    def _live_tids(self, league):
        tids = []
        t = LEAGUES.get(league, {}).get("oddspapi_tid") if pin_source(league) == "oddspapi" else None
        if t:
            tids.append(int(t))
        tids += [int(x) for x in self._extra_tids(league)]
        return tids

    def _live_fetch_oddsapi(self, league, force=False):
        """Odds API branch of live_fetch (nba, euroleague, ncaab, nbl): one Pinnacle call, in-play games KEPT.
        Costs COST_PER_REFRESH credits, at most one call per league every LIVE_PIN_MIN_SEC, at most
        LIVE_ODDSAPI_DAILY_CAP (default 60) credits-worth of calls a day, inside PIN_REF_DAILY_CAP."""
        import time as _t
        key = LEAGUES.get(league, {}).get("odds_api_key")
        if not key:
            return None, "no Odds API source"
        snap = self._live_snap.get(league)
        min_sec = float(os.environ.get("LIVE_PIN_MIN_SEC", 240))
        if snap and not force and _t.time() - snap[0] < min_sec:
            return snap[1], None
        cap = int(os.environ.get("LIVE_ODDSAPI_DAILY_CAP", 60))
        sp = self.data.get("live_oa_spend") or {}
        used_today = int(sp.get("credits", 0)) if sp.get("day") == _now().date().isoformat() else 0
        if used_today + COST_PER_REFRESH > cap or self.spent_today() + COST_PER_REFRESH > self.daily_cap:
            return (snap[1] if snap and _t.time() - snap[0] < 1800 else None), "daily live-check cap reached"
        if self._api is None:
            self._api = OddsAPI()
        raw = self._api.fetch(key, books=[PINNACLE])
        events = []
        for r in raw:
            ev = normalize_event(r, league)
            c = ev.get("commence")
            ev["commence"] = c.isoformat() if c else None
            events.append(ev)
        _now_ = _now()
        _st = [e for e in events if (_parse(e.get("commence")) or _now_ + timedelta(days=1)) <= _now_]
        self.__dict__.setdefault("_live_diag", {}).setdefault(league, {})["Odds API"] = "%d game(s) listed, %d already started, %d of those with Pinnacle prices" % (
            len(events), len(_st), sum(1 for e in _st if e.get("markets")))
        self._add_spend(COST_PER_REFRESH)
        self.data["live_oa_spend"] = {"day": _now().date().isoformat(), "credits": used_today + COST_PER_REFRESH}
        self._live_snap[league] = (_t.time(), events)
        self._live_raw[league] = [dict(start=e.get("commence"), status=None, has_odds=bool(e.get("markets"))) for e in events[:40]]
        self._save()
        return events, None

    def live_fetch(self, league, force=False):
        """(normalized events incl. games in play, or None; why-not text). Odds API leagues: one Pinnacle call (3 credits);
        other leagues: 1-3 OddsPapi calls. At most one fetch per league every LIVE_PIN_MIN_SEC (240); daily caps
        LIVE_ODDSAPI_DAILY_CAP (60 credits) / LIVE_PIN_DAILY_CAP (50 calls), inside PIN_REF_DAILY_CAP / PAPI_DAILY_CAP.
        A league whose first source has no in-play price falls back to the other source when it has one."""
        import time as _t
        why1, oa_events = None, None
        if pin_source(league) != "oddspapi" and LEAGUES.get(league, {}).get("odds_api_key"):
            with self._lock:
                try:
                    events, why1 = self._live_fetch_oddsapi(league, force)
                except Exception as e:
                    events, why1 = None, "Odds API live fetch failed: %s" % e
                    log.warning("live odds api fetch failed for %s: %s", league, e)
            oa_events = events
            if events is not None and any(
                    (e.get("commence") if isinstance(e.get("commence"), datetime) else _parse(e.get("commence")))
                    and (e["commence"] if isinstance(e["commence"], datetime) else _parse(e["commence"])) <= _now()
                    and e.get("markets") for e in events):
                return events, None
            if not self._extra_tids(league) and not LEAGUES.get(league, {}).get("oddspapi_tid"):
                return events, why1
        tids = self._live_tids(league) or ([int(LEAGUES[league]["oddspapi_tid"])] if LEAGUES.get(league, {}).get("oddspapi_tid") else [])
        if not tids:
            return oa_events, why1 or "this league has no OddsPapi source"
        if not os.environ.get("ODDSPAPI_KEYS") and not os.environ.get("ODDSPAPI_KEY"):
            return oa_events, why1 or "no OddsPapi key"
        with self._lock:
            snap = self._live_snap.get(league + ":papi")
            min_sec = float(os.environ.get("LIVE_PIN_MIN_SEC", 240))
            if snap and not force and _t.time() - snap[0] < min_sec:
                return snap[1], None
            cap = int(os.environ.get("LIVE_PIN_DAILY_CAP", 50))
            if self._live_spent_today() + 3 > cap or self._papi_spent_today() + 3 > self.papi_daily_cap:
                return (snap[1] if snap and _t.time() - snap[0] < 1800 else None), "daily live-check cap reached"
            before = self._papi_spent_today()
            raw, normalized, _stats, _ttl = self._papi_fetch_or_empty(tids, league)
            used = max(0, self._papi_spent_today() - before)
            self.data["live_spend"] = {"day": _now().date().isoformat(), "calls": self._live_spent_today() + used}
            self._live_snap[league + ":papi"] = (_t.time(), normalized)
            self._live_raw[league] = [dict(start=f.get("startTime"), status=f.get("statusId"), has_odds=f.get("hasOdds"))
                                      for f in raw[:40]]
            _n_ = _now()
            _stp = [r for r in self._live_raw[league] if (_parse(r.get("start")) or _n_ + timedelta(days=1)) <= _n_]
            self.__dict__.setdefault("_live_diag", {}).setdefault(league, {})["OddsPapi"] = "%d fixture(s) listed, %d already started, %d of those flagged with odds, %d priced event(s) returned (tournaments %s)" % (
                len(raw), len(_stp), sum(1 for r in _stp if r.get("has_odds")), len(normalized), ",".join(str(t) for t in tids))
            self._save()
            return normalized, None

    def live_event(self, ev):
        """(current Pinnacle event of this game in play, None) or (None, why)."""
        events, why = self.live_fetch(ev.get("league"))
        if events is None:
            return None, why
        c = _naive(ev.get("commence"))
        for e in events:
            if not (_same(ev["home"], e["home"]) and _same(ev["away"], e["away"])):
                continue
            pc = e.get("commence")
            pc = pc if isinstance(pc, datetime) else _parse(pc)
            if c and pc and abs(c - pc) > timedelta(hours=24):
                continue
            return e, None
        return None, "game not in the live Pinnacle list"

    def live_probe(self, leagues=None):
        """/livepin: for the given leagues (default: leagues that have a game in play in the cache) fetch fresh and say, per
        game in play, whether the Pinnacle prices are live or frozen at the pre-game numbers."""
        from core import livepin
        now = _now()
        if not leagues:
            leagues = []
            for lg, blob in self.data["leagues"].items():
                for e in blob.get("events", []):
                    pc = _parse(e.get("commence"))
                    if pc and now - timedelta(hours=LIVE_KEEP_HOURS) <= pc <= now and lg not in leagues:
                        leagues.append(lg)
            for lg, blob in (self.data.get("extras") or {}).items():
                for e in blob.get("events", []):
                    pc = _parse(e.get("commence"))
                    if pc and now - timedelta(hours=LIVE_KEEP_HOURS) <= pc <= now and lg not in leagues:
                        leagues.append(lg)
        rows, notes = [], []
        for lg in leagues:
            events, why = self.live_fetch(lg, force=True)
            if events is None:
                notes.append("%s: %s" % (lg.upper(), why))
                continue
            started = [e for e in events if (e.get("commence") if isinstance(e.get("commence"), datetime) else _parse(e.get("commence")))
                       and (e["commence"] if isinstance(e["commence"], datetime) else _parse(e["commence"])) <= now]
            if not started:
                raw = self._live_raw.get(lg) or []
                n_started = sum(1 for r in raw if (_parse(r.get("start")) or now + timedelta(days=1)) <= now)
                diag = getattr(self, "_live_diag", {}).get(lg) or {}
                if n_started:
                    notes.append("%s: %d game(s) started in the feed, none carries Pinnacle prices now (Pinnacle drops its prices at tip-off)" % (lg.upper(), n_started))
                elif diag:   # say what each source returned, so "no live price" is a finding and not a guess
                    notes.append("%s: no in-play Pinnacle price came back." % lg.upper())
                else:   # CLAUDE FIX: nothing is in play, so nothing can be said about live prices
                    notes.append("%s: no game in play right now, so live prices cannot be checked. Try again while a game is live." % lg.upper())
                for src, txt in diag.items():
                    notes.append("  %s: %s" % (src, txt))
                continue
            for e in started:
                pre = None
                for blob in (self.data["leagues"].get(lg), (self.data.get("extras") or {}).get(lg)):
                    for old in (blob or {}).get("events", []):
                        if _same(e["home"], old["home"]) and _same(e["away"], old["away"]):
                            pre = old
                mu_n, t_n, _w = self.main_lines(e)
                mu_p, t_p, _w2 = self.main_lines(pre) if pre else (None, None, "")
                frozen = livepin.frozen(e, pre) if pre else False
                rows.append(dict(league=lg, home=e["home"], away=e["away"],
                                 n_markets=sum(len(v) for v in e.get("markets", {}).values()),
                                 total_now=t_n, total_pre=t_p,
                                 spread_now=None if mu_n is None else -mu_n, spread_pre=None if mu_p is None else -mu_p,
                                 verdict=("FROZEN at pre-game prices: no live price" if frozen else
                                          "LIVE: prices differ from pre-game" if pre else "prices present (no pre-game copy to compare)")))
        return rows, notes

    def _upcoming_plus_live(self, normalized, old_events):
        now = _now()
        events = []
        for ev in normalized:
            c = ev.get("commence")
            if c and c <= now:
                continue
            ev["commence"] = c.isoformat() if c else None
            events.append(ev)
        upcoming = len(events)
        seen = {e.get("event_id") for e in events}
        for old in old_events:
            pc = _parse(old.get("commence"))
            if pc and now - timedelta(hours=LIVE_KEEP_HOURS) <= pc <= now and old.get("event_id") not in seen:
                events.append(old)
        return events, upcoming

    def _papi_fetch_or_empty(self, tids, league):
        """_papi_fetch, but OddsPapi's 404 'no fixture for this tournament' is an ordinary empty answer: the league
        is simply idle right now. Returns (raw, normalized, stats, empty_ttl_min or None)."""
        from services.client import HttpError
        try:
            raw, normalized, stats = self._papi_fetch(tids, league)
            return raw, normalized, stats, None
        except HttpError as e:
            if getattr(e, "status", None) == 404:
                return [], [], {}, float(os.environ.get("PAPI_404_TTL_MIN", 150))
            raise

    def probe(self, tid):
        """/probe: one look at an OddsPapi tournament id. Returns (raw fixtures, normalized events, stats) or raises
        RuntimeError with a plain message (no key / daily cap). Costs 1-3 OddsPapi calls, counted in today's spend."""
        if not os.environ.get("ODDSPAPI_KEYS") and not os.environ.get("ODDSPAPI_KEY"):
            raise RuntimeError("No OddsPapi key is set on this server.")
        if self._papi_spent_today() + 3 > self.papi_daily_cap:
            raise RuntimeError("OddsPapi daily call cap reached (%s/%s); try again tomorrow or raise PAPI_DAILY_CAP."
                               % (self._papi_spent_today(), self.papi_daily_cap))
        raw, normalized, stats, _ttl = self._papi_fetch_or_empty([int(tid)], "probe")
        self._save()
        return raw, normalized, stats

    def tournaments(self):
        """/tournaments: OddsPapi's basketball tournament list (one call)."""
        if not os.environ.get("ODDSPAPI_KEYS") and not os.environ.get("ODDSPAPI_KEY"):
            raise RuntimeError("No OddsPapi key is set on this server.")
        if self._papi_spent_today() + 1 > self.papi_daily_cap:
            raise RuntimeError("OddsPapi daily call cap reached (%s/%s)." % (self._papi_spent_today(), self.papi_daily_cap))
        from services import oddspapi as op
        if self._papi is None:
            self._papi = op.OddsPapi()
        before = self._papi.calls
        try:
            return self._papi.tournaments()
        finally:
            self.data["papi_spend"] = {"day": _now().date().isoformat(),
                                       "calls": self._papi_spent_today() + (self._papi.calls - before)}
            self._save()

    def _refresh_papi(self, league: str) -> int:
        """OddsPapi is the Pinnacle source of this league (no Odds API key)."""
        tid = LEAGUES.get(league, {}).get("oddspapi_tid")
        raw, normalized, stats, ttl = self._papi_fetch_or_empty([tid], league)
        now = _now()
        events, upcoming = self._upcoming_plus_live(normalized, self.data["leagues"].get(league, {}).get("events", []))
        self.data["leagues"][league] = {"fetched_at": now.isoformat(), "events": events, "source": "oddspapi",
                                        "raw_fixtures": len(raw)}
        if ttl:
            self.data["leagues"][league]["ttl_min"] = ttl   # asked, found nothing: ask again after this long
        self._after_refresh(league, events, "main")
        log.info("oddspapi %s: %d fixtures, %d with prices, %d upcoming (calls today %d/%d) %s", league, len(raw),
                 len(normalized), upcoming, self._papi_spent_today(), self.papi_daily_cap,
                 {k: v for k, v in stats.items() if k in ("spread_flipped", "spread_ok", "no_names")})
        self._save()
        return upcoming

    # --- extra OddsPapi tournaments for a league that has an Odds API key (e.g. NBA preseason) ---
    def _extra_tids(self, league):
        return tuple(LEAGUES.get(league, {}).get("oddspapi_extra_tids") or ())

    def _refresh_extras(self, league: str) -> int:
        tids = self._extra_tids(league)
        raw, normalized, stats, ttl = self._papi_fetch_or_empty(tids, league)
        old = (self.data.setdefault("extras", {}).get(league) or {}).get("events", [])
        events, upcoming = self._upcoming_plus_live(normalized, old)
        self.data["extras"][league] = {"fetched_at": _now().isoformat(), "events": events, "source": "oddspapi",
                                       "raw_fixtures": len(raw)}
        if ttl:
            self.data["extras"][league]["ttl_min"] = ttl
        self._after_refresh(league, events, "extra")
        log.info("oddspapi extra %s %s: %d fixtures, %d with prices, %d upcoming (calls today %d/%d)", league, list(tids),
                 len(raw), len(normalized), upcoming, self._papi_spent_today(), self.papi_daily_cap)
        self._save()
        return upcoming

    def _near_tip(self, blob) -> bool:
        """A game of this blob starts within NEAR_TIP_HOURS: prices move most then, so refresh more often."""
        now = _now()
        hrs = float(os.environ.get("PIN_REF_NEAR_TIP_HOURS", 3))
        for e in blob.get("events", []):
            c = _parse(e.get("commence"))
            if c and now <= c <= now + timedelta(hours=hrs):
                return True
        return False

    def _extras_ttl_min(self, blob) -> float:
        if blob.get("ttl_min"):
            return float(blob["ttl_min"])
        if blob.get("source") == "oddspapi":
            return self._papi_ttl_min(blob, float(os.environ.get("PAPI_EXTRA_MAX_AGE_MIN", 120)))
        now = _now()
        horizon = now + timedelta(days=IDLE_HORIZON_DAYS)
        for e in blob.get("events", []):
            c = _parse(e.get("commence"))
            if c and now - timedelta(hours=LIVE_KEEP_HOURS) <= c <= horizon:
                ttl = float(os.environ.get("PAPI_EXTRA_MAX_AGE_MIN", 120))
                return min(ttl, float(os.environ.get("PAPI_NEAR_AGE_MIN", 60))) if self._near_tip(blob) else ttl
        return max(self.max_age_min, self.idle_max_age_min)

    def _ensure_extras(self, league: str) -> bool:
        if not self._extra_tids(league):
            return False
        if not os.environ.get("ODDSPAPI_KEYS") and not os.environ.get("ODDSPAPI_KEY"):
            return False
        blob = (self.data.get("extras") or {}).get(league)
        if blob and self._age_min(blob) < self._extras_ttl_min(blob):
            return True
        if self._papi_spent_today() + 3 > self.papi_daily_cap:
            return bool(blob) and self._age_min(blob) <= self.stale_max_min
        self._refresh_extras(league)
        return True

    def _refresh(self, league: str) -> int:
        """PAID: one Pinnacle fetch for the league. Returns upcoming games stored (0 if the league has no key)."""
        if pin_source(league) == "oddspapi":
            return self._refresh_papi(league)
        key = LEAGUES.get(league, {}).get("odds_api_key")
        if not key:
            log.info("no odds_api_key for %s; no Pinnacle reference", league)
            return 0
        if self._api is None:
            self._api = OddsAPI()
        raw = self._api.fetch(key, books=[PINNACLE])
        now = _now()
        events = []
        for r in raw:
            ev = normalize_event(r, league)
            c = ev.get("commence")
            if c and c <= now:  # already in play: in-play prices are not a prematch reference
                continue
            ev["commence"] = c.isoformat() if c else None
            events.append(ev)
        upcoming = len(events)
        # CLAUDE FIX: a refresh used to delete the Pinnacle lines of games already in play, so live lost its real
        # price in the middle of a game. Keep games that started in the last LIVE_KEEP_HOURS (prematch matching still
        # refuses them; only live matching, allow_started=True, uses them).
        seen = {e.get("event_id") for e in events}
        for old in self.data["leagues"].get(league, {}).get("events", []):
            pc = _parse(old.get("commence"))
            if pc and now - timedelta(hours=LIVE_KEEP_HOURS) <= pc <= now and old.get("event_id") not in seen:
                events.append(old)
        self.data["leagues"][league] = {"fetched_at": now.isoformat(), "events": events}
        self._after_refresh(league, events, "main")
        self._add_spend(COST_PER_REFRESH)
        self._save()
        return upcoming

    def _after_refresh(self, league, events, scope="main"):
        """Tell the engine a league was just refreshed. Never allowed to break a refresh."""
        cb = self.on_refresh
        if cb is None:
            return
        try:
            cb(league, events, scope)
        except Exception as e:
            log.warning("refresh hook failed for %s: %s", league, e)

    def _papi_ttl_min(self, blob, active_ttl) -> float:
        """SMARTER REFRESH for OddsPapi leagues (the scarce, metered source). A league is only worth a call when a game
        starts inside PAPI_ACTIVE_HOURS (24h): then it refreshes every `active_ttl` minutes (45 inside the last 3 hours).
        With nothing in that window it waits PAPI_IDLE_AGE_MIN (480) - the TTL is re-evaluated on every scan, so a game
        that enters the 24h window triggers a refresh at once. Games already in play need no call: the near-tip
        refresh before the start is the reference the live model keeps using (it stays inside stale_max)."""
        now = _now()
        horizon = now + timedelta(hours=float(os.environ.get("PAPI_ACTIVE_HOURS", 24)))
        for e in blob.get("events", []):
            c = _parse(e.get("commence"))
            if c and now <= c <= horizon:
                if self._near_tip(blob):
                    return min(active_ttl, float(os.environ.get("PIN_REF_NEAR_AGE_MIN", 45)))
                return active_ttl
        return max(active_ttl, float(os.environ.get("PAPI_IDLE_AGE_MIN", 480)))

    def _ttl_min(self, blob) -> float:
        """Normal max age while a game is close (or in play); the idle max age when nothing is."""
        if blob.get("ttl_min"):
            return float(blob["ttl_min"])
        if blob.get("source") == "oddspapi":
            return self._papi_ttl_min(blob, self.max_age_min)
        now = _now()
        horizon = now + timedelta(days=IDLE_HORIZON_DAYS)
        for e in blob.get("events", []):
            c = _parse(e.get("commence"))
            if c and now - timedelta(hours=LIVE_KEEP_HOURS) <= c <= horizon:
                if self._near_tip(blob):
                    return min(self.max_age_min, float(os.environ.get("PIN_REF_NEAR_AGE_MIN", 45)))
                return self.max_age_min
        return max(self.max_age_min, self.idle_max_age_min)

    def ensure_fresh(self, league: str) -> bool:
        with self._lock:
            return self._ensure_fresh(league)

    def league_age_min(self, league: str) -> float:
        """Minutes since the newest Pinnacle pull that can price a game of this league (inf when none)."""
        ages = [self._age_min(b) for b in (self.data["leagues"].get(league), (self.data.get("extras") or {}).get(league))
                if b]
        return min(ages) if ages else float("inf")

    def force_fresh(self, league: str):
        """Pull this league's Pinnacle prices NOW (T-60 / T-30 confirmations). Returns the age in minutes of the newest
        data afterwards. Skips when the data is already younger than PIN_FORCE_MIN_AGE_MIN (games of one league share a
        pull) or when today's forced pulls reached PIN_FORCE_CAP; retries once on a failure."""
        with self._lock:
            age = self.league_age_min(league)
            if age <= float(os.environ.get("PIN_FORCE_MIN_AGE_MIN", 8)):
                return age
            today = _now().date().isoformat()
            fs = self.data.get("force_spend") or {}
            n = int(fs.get("n", 0)) if fs.get("day") == today else 0
            if n >= int(os.environ.get("PIN_FORCE_CAP", 40)):
                log.warning("forced Pinnacle pulls reached the daily cap (%d); %s stays %.0f min old", n, league, age)
                return age
            src = pin_source(league)
            if src is None:
                return age
            if src == "oddspapi":
                if not os.environ.get("ODDSPAPI_KEYS") and not os.environ.get("ODDSPAPI_KEY"):
                    return age
                if self._papi_spent_today() + 3 > self.papi_daily_cap:
                    log.warning("forced pull of %s blocked: oddspapi daily call cap reached", league)
                    return age
            elif self.spent_today() + COST_PER_REFRESH > self.daily_cap:
                return age
            self.data["force_spend"] = {"day": today, "n": n + 1}
            for attempt in (1, 2):
                try:
                    self._refresh(league)
                    break
                except Exception as e:
                    log.warning("forced Pinnacle pull of %s failed (try %d): %s", league, attempt, e)
            try:
                self._ensure_extras(league)
            except Exception as e:
                log.warning("forced extra Pinnacle pull of %s failed: %s", league, e)
            self._save()
            return self.league_age_min(league)

    def _ensure_fresh(self, league: str) -> bool:
        ok = self._ensure_main(league)
        try:  # extras (OddsPapi games the main source lacks) must never break the main refresh
            extra = self._ensure_extras(league)
        except Exception as e:
            log.warning("extra Pinnacle source for %s failed: %s", league, e)
            extra = False
        return ok or extra

    def _ensure_main(self, league: str) -> bool:
        """Refresh only when the cached data is old AND the daily credit cap allows. True if data is usable."""
        blob = self.data["leagues"].get(league)
        # OddsPapi league that HAS fixtures but no usable price (or an old blob from before this field existed):
        # retry after 45 min instead of trusting an empty cache for the full TTL
        retry_empty = bool(blob) and blob.get("source") == "oddspapi" and not blob.get("events") \
            and blob.get("raw_fixtures") != 0 and self._age_min(blob) >= PAPI_EMPTY_RETRY_MIN
        if blob and not retry_empty and self._age_min(blob) < self._ttl_min(blob):
            return True
        src = pin_source(league)
        if src is None:
            return False
        if src == "oddspapi":
            if not os.environ.get("ODDSPAPI_KEYS") and not os.environ.get("ODDSPAPI_KEY"):
                return False
            if self._papi_spent_today() + 3 > self.papi_daily_cap:
                log.warning("oddspapi daily call cap reached (%s/%s); skipping %s", self._papi_spent_today(),
                            self.papi_daily_cap, league)
                return bool(blob) and self._age_min(blob) <= self.stale_max_min
            self._refresh(league)
            return True
        if self.spent_today() + COST_PER_REFRESH > self.daily_cap:
            log.warning("pinref daily credit cap reached (%s/%s); skipping %s", self.spent_today(), self.daily_cap, league)
            return bool(blob) and self._age_min(blob) <= self.stale_max_min
        self._refresh(league)
        return True

    # --- matching ---
    def match(self, ev, allow_started=False):
        pool = []
        for blob in (self.data["leagues"].get(ev.get("league")), (self.data.get("extras") or {}).get(ev.get("league"))):
            if blob and self._age_min(blob) <= self.stale_max_min:
                pool += blob.get("events", [])
        if not pool:
            return None
        c, now = _naive(ev.get("commence")), _now()
        best, best_score, found = None, 0, []
        for p in pool:
            if not (_same(ev["home"], p["home"]) and _same(ev["away"], p["away"])):
                continue
            pc = _parse(p.get("commence"))
            if pc and pc <= now and not allow_started:
                continue
            if c and pc and abs(c - pc) > timedelta(hours=24):
                continue
            score = (_compact(ev["home"]) == _compact(p["home"])) + (_compact(ev["away"]) == _compact(p["away"])) + 1
            found.append((score, p))
            if score > best_score:
                best, best_score = p, score
        if best is None or len(found) < 2:
            return best
        # The same game can sit in two sources (Odds API main lines + OddsPapi quarter / half lines). Keep the best
        # match as it is and ADD the markets only the other source has; a market both have is never overwritten.
        merged = dict(best, markets={k: list(v) for k, v in best.get("markets", {}).items()})
        for _s, other in sorted(found, key=lambda t: -t[0]):
            if other is best:
                continue
            for k, outs in (other.get("markets") or {}).items():
                merged["markets"].setdefault(k, list(outs))
        return merged

    def refresh_id(self, league: str) -> str:
        """Changes whenever Pinnacle data of this league is refreshed (main or extra source): the position tracker counts
        separate refreshes with it."""
        a = (self.data["leagues"].get(league) or {}).get("fetched_at") or ""
        b = ((self.data.get("extras") or {}).get(league) or {}).get("fetched_at") or ""
        return "%s|%s" % (a, b)

    def pregame_parts(self, ev):
        """(expected home margin or None, expected total or None, why) from the REAL Pinnacle main lines of this game.
        Works for games already in play (cached pre-game lines). Main line = the pair with prices closest to even.
        why says what is missing when a part is None: 'no cached game', 'no spread', 'no total'."""
        p = self.match(ev, allow_started=True)
        if not p:
            return None, None, "no cached Pinnacle game (league %s)" % ev.get("league")
        return self.main_lines(p)

    @staticmethod
    def main_lines(p):
        """(expected home margin, expected total, why) read straight from one Pinnacle event dict (no cache lookup)."""
        groups, best = {}, {}
        for m in ("spreads", "totals"):
            for o in p["markets"].get(m, []):
                if o.get("book") == PINNACLE:
                    groups.setdefault((m, _ln(o.get("line"))), {})[o["selection"]] = o["odds"]
        for (m, ln), sides in groups.items():
            a_s, b_s = _TWO_WAY[m]
            if ln is None or a_s not in sides or b_s not in sides:
                continue
            gap = abs(sides[a_s] - sides[b_s])
            if m not in best or gap < best[m][0]:
                best[m] = (gap, ln)
        mu = -best["spreads"][1] if "spreads" in best else None
        T = best["totals"][1] if "totals" in best else None
        why = "" if mu is not None and T is not None else ("no Pinnacle total" if T is None and mu is not None else
                                                           "no Pinnacle spread" if mu is None and T is not None else "no Pinnacle spread or total")
        return mu, T, why

    def pregame_params(self, ev):
        """(expected home margin, expected total) from the REAL Pinnacle main lines of this game, or None (both are needed)."""
        mu, T, _why = self.pregame_parts(ev)
        return (mu, T) if mu is not None and T is not None else None

    def rows_for(self, ev):
        p = self.match(ev)
        return p["markets"] if p else []

    # --- data agreement check ---
    @staticmethod
    def _mains(markets, pinnacle: bool):
        """(main spread line (home), main total line) of one side of a game: the pair priced closest to even."""
        groups, best = {}, {}
        for m in ("spreads", "totals"):
            for o in (markets or {}).get(m, []) or []:
                if (o.get("book") == PINNACLE) == pinnacle and o.get("odds"):
                    groups.setdefault((m, _ln(o.get("line"))), {})[o.get("selection")] = float(o["odds"])
        for (m, ln), sides in groups.items():
            a_s, b_s = _TWO_WAY[m]
            if ln is None or a_s not in sides or b_s not in sides:
                continue
            gap = abs(sides[a_s] - sides[b_s])
            if m not in best or gap < best[m][0]:
                best[m] = (gap, ln)
        return (best["spreads"][1] if "spreads" in best else None, best["totals"][1] if "totals" in best else None)

    def mismatch(self, ev):
        """Why the SportyBet game and the Pinnacle game cannot be trusted to be the same game and market, or None.
        Normal gaps between the books are 0-2 points. A total more than DATA_TOTAL_GAP (6%) apart, a spread more than
        DATA_SPREAD_GAP (5 pts) apart, or opposite favourites (both 3+ pts) means a wrong match or a mis-filed line:
        no pick is made from such a game."""
        try:
            pin = self.match(ev)
            if not pin:
                return None
            ss, st = self._mains(ev.get("markets"), False)
            ps, pt = self._mains(pin.get("markets"), True)
            tg = float(os.environ.get("DATA_TOTAL_GAP", 0.06))
            sg = float(os.environ.get("DATA_SPREAD_GAP", 5.0))
            if st is not None and pt is not None and abs(st - pt) > tg * max(st, pt):
                return "totals far apart (SportyBet %.1f, Pinnacle %.1f)" % (st, pt)
            if ss is not None and ps is not None:
                if ss * ps < 0 and abs(ss) >= 3 and abs(ps) >= 3:
                    return "the two books disagree on the favourite (SportyBet %+.1f, Pinnacle %+.1f)" % (ss, ps)
                if abs(ss - ps) > sg:
                    return "spreads far apart (SportyBet %+.1f, Pinnacle %+.1f)" % (ss, ps)
        except Exception:
            return None
        return None

    # --- candidates ---
    def table_rows(self, ev) -> list:
        """Every full-game SportyBet side that has a Pinnacle fair price on the same line, qualifying or not (feeds the price table
        on the match card; unlike candidates() nothing is filtered out and nothing is sent anywhere)."""
        pin = self.match(ev)
        if not pin or self.mismatch(ev):
            return []
        fair = fair_probs(pin)
        groups = {}
        for m, outs in ev.get("markets", {}).items():
            if m not in ("h2h", "spreads", "totals"):
                continue
            for o in outs:
                if o["book"] == PINNACLE:
                    continue
                groups.setdefault((o["book"], m, _ln(o.get("line"))), {})[o["selection"]] = o["odds"]
        out = []
        for (book, m, ln), sides in groups.items():
            f = fair.get((m, ln))
            if not f:
                continue
            for sel, odds in sides.items():
                p = f.get(sel)
                if not p or odds <= 1:
                    continue
                out.append(dict(market=m, selection=sel, line=ln, odds=odds, p=p, fair_odds=round(1 / p, 3), ev=edge(p, odds), book=book))
        return out

    def candidates(self, ev, near=None) -> list:
        """near: optional list that also receives edges that did NOT qualify (digest 'closest to qualifying')."""
        pin = self.match(ev)
        if not pin:
            return []
        bad = self.mismatch(ev)
        if bad:
            log.warning("data check failed for %s v %s (%s): %s", ev.get("home"), ev.get("away"), ev.get("league"), bad)
            return []
        fair = fair_probs(pin)
        groups = {}
        for m, outs in ev.get("markets", {}).items():
            for o in outs:
                if o["book"] == PINNACLE:
                    continue
                groups.setdefault((o["book"], m, _ln(o.get("line"))), {})[o["selection"]] = o["odds"]
        min_ev = settings.MIN_EV
        out = []
        for (book, m, ln), sides in groups.items():
            f = fair.get((m, ln))
            if not f:
                continue
            a_s, b_s = _TWO_WAY[m]
            margin = (1 / sides[a_s] + 1 / sides[b_s] - 1) if (a_s in sides and b_s in sides) else None
            for sel, odds in sides.items():
                p = f.get(sel)
                if not p or odds <= 1:
                    continue
                e = edge(p, odds)
                if near is not None and -0.03 <= e <= MAX_EV_SANITY:
                    near.append(dict(league=ev["league"], event_id=ev["event_id"], home=ev["home"], away=ev["away"],
                                     market=m, selection=sel, line=ln, odds=odds, p=p, ev=e, book_margin=margin))
                if e < min_ev or e > MAX_EV_SANITY:
                    continue
                out.append(dict(
                    league=ev["league"], event_id=ev["event_id"], home=ev["home"], away=ev["away"],
                    commence=ev.get("commence"), market=m, selection=sel, side=sel, line=ln, odds=odds,
                    p=p, fair_odds=round(1 / p, 3), min_odds=round((1 + min_ev) / p, 3),
                    book=book, book_margin=margin, ev=e))
        return out
