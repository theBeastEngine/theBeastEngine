"""/scout: which basketball leagues could we ADD? Read-only: nothing is saved, no alert is sent.
A league qualifies only when BOTH are true right now:
  1. OddsPapi has a tournament for it with games AND Pinnacle really prices them (a probe of that tournament), and
  2. SportyBet lists games of it (read from SportyBet's own feed), so there is a price to compare with.
Cost: 1 OddsPapi call for the tournament list + 1-3 per candidate that has games (the daily cap is respected).
/scout            all candidates          /scout italy        only the candidates whose name has 'italy'
/scout <words>    a league that is not on the list: the words are used to search both sides.
/scout auto       starts from SportyBet's own list of competitions we do not cover, then asks whether Pinnacle prices them
/scout list       just that SportyBet list (no OddsPapi call)"""
from __future__ import annotations

import re

from services.logger import get_logger

log = get_logger("scout")

# (label, [OddsPapi word lists, any of them], [SportyBet word combos, any of them], excluded words, code)
CANDIDATES = [
    ("EuroCup", [["eurocup"]], [["eurocup"]], ["women"], "eurocup"),
    ("Italy Lega Basket Serie A", [["italy", "lega"], ["italy", "serie"]], [["italy", "serie a"], ["italy", "lega"], ["italy", "lba"]], ["women", "a2", "serie b", "femminile"], "ita"),
    ("Greece A1", [["greece", "a1"], ["greece", "basket", "league"], ["greece", "basketball"]], [["greece", "a1"], ["greece", "basket league"], ["greece", "basket"]], ["women", "a2", "cup"], "gre"),
    ("ABA League (Adriatic)", [["aba"], ["adriatic"]], [["aba league"], ["adriatic"]], ["women", "aba 2", "junior"], "aba"),
    ("Israel Super League", [["israel", "super"], ["israel", "winner"], ["israel", "league"]], [["israel", "super"], ["israel", "winner"], ["israel", "league"]], ["women", "national", "cup"], "isr"),
    ("Basketball Champions League", [["champions", "league"]], [["champions league"]], ["women", "americas", "asia", "fiba", "qualification", "qualifier"], "bcl"),
    ("Lithuania LKL", [["lithuania", "lkl"], ["lithuania"]], [["lithuania", "lkl"], ["lithuania"]], ["women", "nkl", "cup", "u"], "ltu"),
    ("VTB United League", [["vtb"], ["united", "league"]], [["vtb"], ["united league"]], ["women", "youth"], "vtb"),
]
SPORTY_PAGES = 8
MAX_PROBES = 8


def _toks(*parts):
    return re.findall(r"[a-z0-9]+", " ".join(str(p or "") for p in parts).lower())


def _hit(w, tokens):
    """Whole-word match only: a prefix rule let 'champions' match 'championship' (a British league)."""
    return w in tokens


def _excluded(exclude, toks):
    """An excluded word or a two-word phrase ('serie b') found in the tokens."""
    joined = " " + " ".join(toks) + " "
    return any((" %s " % " ".join(_toks(x))) in joined for x in exclude)


def papi_matches(rows, wordlists, exclude=()):
    """OddsPapi tournament rows that fit any word list: [(tid, category, name, fixtures)], busiest first."""
    out = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        tid = r.get("tournamentId", r.get("id"))
        name, cat = r.get("tournamentName", r.get("name")) or "", r.get("categoryName", r.get("category")) or ""
        if tid is None or not name:
            continue
        toks = _toks(cat, name)
        if _excluded(exclude, toks):
            continue
        if not any(all(_hit(w, toks) for w in wl) for wl in wordlists):
            continue
        parts = [r.get(k) for k in ("liveFixtures", "upcomingFixtures", "futureFixtures")]
        n = int(sum(v for v in parts if isinstance(v, (int, float)) and not isinstance(v, bool)))
        out[int(tid)] = (int(tid), str(cat), str(name), n)
    return sorted(out.values(), key=lambda x: -x[3])


def sporty_matches(tournaments, combos, exclude=()):
    """SportyBet tournaments [(category, name, n_events)] that fit any combo of phrases (whole words): busiest first."""
    from core.bookmakers._base import _BAD, _ascii, _has
    res = []
    for cat, name, n in tournaments or []:
        text = _ascii(("%s %s" % (cat, name)).lower())
        if any(_has(text, b) for b in _BAD) or any(_has(text, x) for x in exclude):
            continue
        if any(all(_has(text, t) for t in combo) for combo in combos):
            res.append((str(cat), str(name), int(n)))
    return sorted(res, key=lambda x: -x[2])


def sporty_tournaments(pages=SPORTY_PAGES):
    """Every basketball tournament on SportyBet's pre-match feed: [(category, name, events)]. Same endpoint as the engine."""
    from core.bookmakers import sportybet as sb
    from core.bookmakers import soft_feed

    async def go():
        seen = {}
        for page in range(1, pages + 1):
            payload = await sb.get_json(sb.URL, headers=sb.HEADERS, params={
                "sportId": "sr:sport:2", "marketId": "219", "pageSize": 100, "pageNum": page, "todayGames": "false", "timeline": ""})
            ts = ((payload or {}).get("data") or {}).get("tournaments") or []
            if not ts:
                break
            for t in ts:
                k = (str(t.get("categoryName", "")), str(t.get("name", "")))
                seen[k] = seen.get(k, 0) + len(t.get("events") or [])
        return [(c, n, e) for (c, n), e in seen.items()]
    return soft_feed._run(go())


def uncovered(tournaments):
    """SportyBet basketball tournaments that none of the 14 leagues covers and that are not women / youth / virtual:
    [(category, name, events)], busiest first. These are what we could bet on if Pinnacle prices them."""
    from core.bookmakers._base import _BAD, _ascii, _has, infer_league
    out = []
    for cat, name, n in tournaments or []:
        text = _ascii(("%s %s" % (cat, name)).lower())
        if any(_has(text, b) for b in _BAD) or infer_league(text):
            continue
        out.append((str(cat), str(name), int(n)))
    return sorted(out, key=lambda x: -x[2])


_GENERIC = {"league", "basketball", "division", "liga", "ligue", "lig", "championship", "national", "first", "premier", "a"}


def papi_for_sporty(rows, cat, name):
    """Best OddsPapi tournament for one SportyBet tournament: same country AND all the distinctive name words."""
    ct = set(_toks(cat))
    words = [w for w in _toks(name) if w not in _GENERIC and len(w) > 1] or _toks(name)
    best = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        tid = r.get("tournamentId", r.get("id"))
        n, c = r.get("tournamentName", r.get("name")) or "", r.get("categoryName", r.get("category")) or ""
        if tid is None or not n:
            continue
        rt, rc = set(_toks(n)), set(_toks(c))
        if not (ct & rc) or not words or not all(w in rt or w in rc for w in words):
            continue
        parts = [r.get(k) for k in ("liveFixtures", "upcomingFixtures", "futureFixtures")]
        best.append((int(tid), str(c), str(n), int(sum(v for v in parts if isinstance(v, (int, float)) and not isinstance(v, bool)))))
    return sorted(best, key=lambda x: -x[3])


def run_auto(pr, sporty=None, top=6):
    """/scout auto: start from what SportyBet really lists (the leagues we could bet on), then ask whether Pinnacle prices them."""
    try:
        rows = pr.tournaments()
    except Exception as e:
        return "Scout stopped: the OddsPapi tournament list could not be read (%s)." % e
    try:
        sb = (sporty or sporty_tournaments)()
    except Exception as e:
        return "Scout stopped: SportyBet's feed could not be read (%s)." % str(e)[:80]
    cands = uncovered(sb)
    out = ["LEAGUE SCOUT, from SportyBet's list (read-only)",
           "SportyBet lists %d basketball competition(s) that none of our 14 leagues covers; checking the %d busiest." % (len(cands), min(top, len(cands)))]
    ok, probes = [], 0
    for cat, name, n in cands[:top]:
        papi = papi_for_sporty(rows, cat, name)
        result, err = None, None
        if papi and papi[0][3] > 0 and probes < MAX_PROBES:
            probes += 1
            try:
                result = pr.probe(papi[0][0])
            except Exception as e:
                err = str(e)[:90]
        verdict, lines = assess("%s %s" % (cat, name), (_toks(cat) or ["new"])[0][:3], papi, result, [(cat, name, n)], err)
        out += ["", "%s %s - %s (%d game(s) on SportyBet)" % ("\u2705" if verdict == "OK" else "\u274C", cat, name, n)]
        out += ["  " + l for l in lines if not l.startswith("SportyBet")]
        if verdict == "OK":
            ok.append("%s %s" % (cat, name))
    rest = cands[top:top + 12]
    if rest:
        out += ["", "Also on SportyBet, not checked yet: " + "; ".join("%s %s (%d)" % r for r in rest)]
    out += ["", "Qualify now: %s." % (", ".join(ok) if ok else "none"),
            "Nothing was added. Reply with the ones you approve."]
    return "\n".join(out)


def run_list(sporty=None):
    """/scout list: SportyBet's competitions that no built-in league covers. No OddsPapi call."""
    try:
        sb = (sporty or sporty_tournaments)()
    except Exception as e:
        return "SportyBet's feed could not be read (%s)." % str(e)[:80]
    c = uncovered(sb)
    if not c:
        return "SportyBet lists no competition outside our 14 leagues right now."
    return "SportyBet basketball competitions outside our 14 leagues (%d):\n" % len(c) + "\n".join(
        "%s - %s (%d)" % r for r in c[:40])


def _kinds(events):
    ks = set()
    for e in events or []:
        ks.update((e.get("markets") or {}).keys())
    return ks


def assess(label, code, papi, probe_result, sporty, probe_error=None):
    """One candidate -> (verdict, [lines]). verdict: 'OK' | 'NO'."""
    lines = []
    ok_p = ok_s = False
    if probe_error:
        lines.append("Pinnacle: could not check (%s)" % probe_error)
    elif not papi:
        lines.append("Pinnacle: no OddsPapi tournament found")
    else:
        tid, cat, name, nfix = papi[0]
        events = (probe_result or [None, []])[1] if probe_result else []
        priced = len(events or [])
        if not nfix and not probe_result:
            lines.append("Pinnacle: %s %s (tid %s) has no games right now" % (cat, name, tid))
        elif priced == 0:
            lines.append("Pinnacle: tid %s %s has %s fixture(s) but no Pinnacle price yet" % (tid, name, nfix))
        else:
            ok_p = True
            ks = _kinds(events)
            q = sorted(k.split("_", 1)[1] for k in ks if "_" in k)
            lines.append("Pinnacle: tid %s %s, %d game(s) priced (%s)%s" % (
                tid, name, priced, ", ".join(sorted(k for k in ks if "_" not in k)),
                ", also " + "/".join(q) + " lines" if q else ""))
        if len(papi) > 1 and ok_p:
            lines.append("  (other tournaments matched: %s - check /tournaments)" % ", ".join("%s tid %s" % (p[2], p[0]) for p in papi[1:3]))
    if sporty:
        ok_s = True
        c, n, ev = sporty[0]
        lines.append("SportyBet: %s - %s, %d game(s) listed" % (c, n, ev))
    else:
        lines.append("SportyBet: no games listed")
    verdict = "OK" if ok_p and ok_s else "NO"
    if verdict == "OK":
        tid = papi[0][0]
        words = "+".join((_toks(sporty[0][0])[:1] + _toks(sporty[0][1])[:2])) or code
        lines.append("To add: /addleague %s \"%s\" tid=%s match=%s" % (code, label, tid, words))
    return verdict, lines


def run(pr, arg="", sporty=None, candidates=None):
    """Whole /scout text. pr: the Pinnacle reference (tournaments(), probe(tid)). sporty: callable -> tournaments (tests)."""
    arg = (arg or "").strip().lower()
    if arg == "auto":
        return run_auto(pr, sporty)
    if arg == "list":
        return run_list(sporty)
    cands = list(candidates if candidates is not None else CANDIDATES)
    if arg:
        sel = [c for c in cands if arg in c[0].lower() or arg == c[4]]
        if not sel:
            w = _toks(arg)
            sel = [("Custom: %s" % arg, [w], [[" ".join(w)]], ["women"], (w[0][:3] if w else "new"))]
        cands = sel
    try:
        rows = pr.tournaments()
    except Exception as e:
        return "Scout stopped: the OddsPapi tournament list could not be read (%s)." % e
    try:
        sb_rows = (sporty or sporty_tournaments)()
    except Exception as e:
        sb_rows, sb_err = [], str(e)[:80]
    else:
        sb_err = None
    out = ["LEAGUE SCOUT (read-only)", "A league qualifies when Pinnacle prices its games AND SportyBet lists them."]
    if sb_err:
        out.append("SportyBet feed could not be read (%s): the SportyBet side shows 'no games' only because of that." % sb_err)
    ok, probes = [], 0
    for label, wl, combos, exc, code in cands:
        papi = papi_matches(rows, wl, exc)
        result, err = None, None
        if papi and papi[0][3] > 0 and probes < MAX_PROBES:
            probes += 1
            try:
                result = pr.probe(papi[0][0])
            except Exception as e:
                err = str(e)[:90]
        sp = sporty_matches(sb_rows, combos, exc)
        verdict, lines = assess(label, code, papi, result, sp, err)
        out.append("")
        out.append("%s %s" % ("✅" if verdict == "OK" else "❌", label))
        out += ["  " + l for l in lines]
        if verdict == "OK":
            ok.append(label)
    out.append("")
    out.append("Qualify now: %s." % (", ".join(ok) if ok else "none"))
    out.append("Nothing was added. Reply with the ones you approve and I will add them (or use the /addleague line shown).")
    return "\n".join(out)
