"""/papiprobe: two read-only probes of OddsPapi's newer API (v5), run on demand (about 5-7 calls in all).
 A. IN-PLAY PINNACLE: /fixtures/live lists the basketball games in play (score, clock, Pinnacle flags); for up to 3 of
    them /fixtures/odds gives the Pinnacle prices. A price whose changedAt is AFTER tip-off moved in play = LIVE.
 B. GRADING: /fixtures/settlement for up to 3 finished games of OddsPapi leagues (ACB, BBL, LNB, PBA...): does it give the
    final score and the won/lost result, and how soon after the game?
Nothing is saved, nothing is sent as an alert. A failure of any call is reported in words, never raised."""
from __future__ import annotations

import html
import time
from datetime import datetime, timedelta, timezone

from services import oddspapi as op
from services.client import HttpError
from services.logger import get_logger

log = get_logger("papiprobe")
MAX_ODDS = 3
MAX_SETTLE = 3


def _err(e) -> str:
    if isinstance(e, HttpError):
        return "HTTP %s %s" % (e.status, str(e)[:110].replace("\n", " "))
    if isinstance(e, op.NoKeys):
        return "v5 rejected the key(s) tried (invalid apiKey): v5 needs its own key, the v4 keys are not accepted"
    return "%s: %s" % (type(e).__name__, str(e)[:100])


def _score(fx) -> str:
    r = (fx.get("scores") or {}).get("result") or {}
    if r.get("participant1Score") is None:
        return "no score"
    return "%s-%s" % (r.get("participant1Score"), r.get("participant2Score"))


def _clock(fx) -> str:
    c = fx.get("clock") or {}
    if not c or c.get("currentTime") is None:
        return "no clock"
    return "P%s %s%s" % (c.get("currentPeriod"), c.get("currentTime"), " (stopped)" if c.get("stopped") else "")


def _names(fx) -> str:
    p = fx.get("participants") or {}
    return "%s vs %s" % (p.get("participant1Name", "?"), p.get("participant2Name", "?"))


def _tour(fx) -> str:
    return str((fx.get("tournament") or {}).get("tournamentName") or "?")


def probe_live(client, now=None) -> list:
    """Probe A. Returns text lines."""
    now = now or time.time()
    out = ["<b>A. In-play Pinnacle (OddsPapi v5)</b>"]
    try:
        fixtures = client.call("/fixtures/live", {"sportId": op.BASKETBALL, "bookmakers": "pinnacle"}, base=op.BASE5)
    except Exception as e:
        out.append("fixtures/live failed: " + _err(e))
        return out
    if not isinstance(fixtures, list):
        out.append("fixtures/live returned an unexpected shape: %s" % type(fixtures).__name__)
        return out
    live = [f for f in fixtures if (f.get("status") or {}).get("statusId") == 1 or (f.get("status") or {}).get("live")]
    out.append("%d basketball game(s) in play in the feed." % len(live))
    with_odds = []
    for f in live[:12]:
        pin = (f.get("bookmakers") or {}).get("pinnacle") or {}
        flags = "Pinnacle: no odds" if not pin.get("hasOdds") else "Pinnacle odds%s%s" % (
            ", STALE" if pin.get("staleOdds") else "", ", SUSPENDED" if pin.get("suspended") else "")
        out.append("- %s | %s | %s | %s | %s" % (html.escape(_tour(f)), html.escape(_names(f)), _score(f), _clock(f), flags))
        if pin.get("hasOdds"):
            with_odds.append(f)
    if not live:
        out.append("Nothing in play, so no price can be tested. Run /papiprobe again while a game is live.")
        return out
    if not with_odds:
        out.append("Verdict: games are in play but Pinnacle carries no odds on any of them.")
        return out
    verdicts = []
    for f in with_odds[:MAX_ODDS]:
        name = "%s | %s" % (_tour(f), _names(f))
        try:
            data = client.call("/fixtures/odds", {"fixtureId": f.get("fixtureId"), "bookmakers": "pinnacle"}, base=op.BASE5)
        except Exception as e:
            out.append("fixtures/odds for %s failed: %s" % (html.escape(name), _err(e)))
            continue
        quotes = list(((data or {}).get("odds") or {}).get("pinnacle", {}).values()) if isinstance(data, dict) else []
        active = [q for q in quotes if q.get("active") and q.get("marketActive")]
        main = [q for q in active if q.get("mainLine")]
        ch = [q.get("changedAt") for q in quotes if q.get("changedAt")]
        start = f.get("startTime")
        if not ch or not start:
            out.append("- %s: %d quotes, %d active, %d main-line; no timestamps to judge" % (html.escape(name), len(quotes), len(active), len(main)))
            continue
        newest = max(ch) / 1000.0
        moved = sum(1 for c in ch if c / 1000.0 > float(start) + 60)
        age = int(now - newest)
        v = "LIVE" if moved else "FROZEN at tip-off"
        verdicts.append(v)
        out.append("- %s: %d quotes, %d active, %d main-line; %d changed after tip-off; newest change %d s ago -> %s" % (
            html.escape(name), len(quotes), len(active), len(main), moved, age, v))
    if verdicts:
        out.append("Verdict: %s." % ("Pinnacle in-play prices ARE available through OddsPapi v5 (%d of %d tested game(s) live)" % (
            verdicts.count("LIVE"), len(verdicts)) if "LIVE" in verdicts else "the tested games carry only frozen pre-game prices"))
    return out


def _candidates(beast, now=None):
    """[(label, fixtureId)] finished or started games of OddsPapi leagues from the pick log and the Pinnacle cache."""
    from config.leagues import pin_source
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    seen, out = set(), []

    def add(label, eid, commence):
        if not str(eid or "").startswith("papi-") or eid in seen:
            return
        c = commence
        if isinstance(c, str):
            try:
                c = datetime.fromisoformat(c.replace("Z", "+00:00"))
            except ValueError:
                c = None
        if c is not None and c.tzinfo is not None:
            c = c.astimezone(timezone.utc).replace(tzinfo=None)
        if c is None or not (now - timedelta(days=3) <= c <= now - timedelta(hours=2)):
            return
        seen.add(eid)
        out.append((label, str(eid)[5:], c))

    try:
        from storage import repository as repo
        for p in repo.all_picks(600):
            if pin_source(p.get("league")) == "oddspapi":
                add("%s | %s vs %s" % (str(p.get("league")).upper(), p.get("home"), p.get("away")), p.get("event_id"), p.get("commence"))
    except Exception as e:
        log.info("papiprobe: pick log not read: %s", e)
    try:
        data = beast.pinref.data
        for group in (data.get("leagues") or {}, data.get("extras") or {}):
            for lg, blob in group.items():
                for e in (blob or {}).get("events", []):
                    add("%s | %s vs %s" % (str(lg).upper(), e.get("home"), e.get("away")), e.get("event_id"), e.get("commence"))
    except Exception as e:
        log.info("papiprobe: cache not read: %s", e)
    out.sort(key=lambda x: x[2], reverse=True)
    return out


def probe_settle(client, beast, now=None) -> list:
    """Probe B. Returns text lines."""
    out = ["", "<b>B. Settlement for grading (OddsPapi v5)</b>"]
    try:
        cands = _candidates(beast, now)
    except Exception as e:
        out.append("could not list finished games: " + _err(e))
        return out
    if not cands:
        out.append("No finished game of an OddsPapi league is in the pick log or cache yet (needs a game that ended 2 h to 3 days ago). Run again after tonight's games.")
        return out
    ok = 0
    for label, fid, _c in cands[:MAX_SETTLE]:
        try:
            data = client.call("/fixtures/settlement", {"fixtureId": fid}, base=op.BASE5)
        except Exception as e:
            out.append("- %s: %s" % (html.escape(label), _err(e)))
            continue
        if not isinstance(data, dict):
            out.append("- %s: unexpected shape %s" % (html.escape(label), type(data).__name__))
            continue
        st = data.get("settlements") or []
        counts = {}
        for s in st:
            counts[s.get("status")] = counts.get(s.get("status"), 0) + 1
        sid = (data.get("status") or {}).get("statusName")
        out.append("- %s: status %s, final %s, %d settled outcome(s) %s" % (
            html.escape(label), sid, _score(data), len(st), counts or ""))
        if st and _score(data) != "no score":
            ok += 1
    out.append("Verdict: %s." % ("settlement works: final score and per-outcome results are available, so ACB/BBL/LNB/PBA picks can be graded" if ok
                                 else "no usable settlement came back yet (see lines above)"))
    return out


def v5_client():
    """A SEPARATE client for the probe, never the engine's own: a key v5 rejects must not retire the v4 keys the engine uses.
    ODDSPAPI_V5_KEYS (comma separated) if set, else at most 2 of the v4 keys, enough to learn whether v5 accepts them."""
    import os
    raw = os.environ.get("ODDSPAPI_V5_KEYS", "")
    keys = [k.strip() for k in raw.replace(";", ",").split(",") if k.strip()] or op.keys_from_env()[:2]
    return op.OddsPapi(keys=keys)


def run(beast, arg: str = "", client=None, now=None) -> str:
    """Whole /papiprobe text. arg: 'live' or 'settle' to run only one probe."""
    mode = (arg or "").strip().lower()
    if client is None:
        client = v5_client()
    lines = ["<b>OddsPapi probe</b> (read-only)"]
    try:
        if mode in ("", "live"):
            lines += probe_live(client, now and now.timestamp() if hasattr(now, "timestamp") else None)
        if mode in ("", "settle"):
            lines += probe_settle(client, beast, now)
    except Exception as e:
        log.warning("papiprobe failed: %s", e)
        lines.append("probe stopped: " + _err(e))
    return "\n".join(lines)
