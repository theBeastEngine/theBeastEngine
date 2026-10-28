"""Leagues added from Telegram (/addleague) without a code push.

An added league is stored in the kv table (key 'leagues_extra') and merged into the live config dictionaries IN PLACE
at start-up and at once when added, so every module that already imported LEAGUES / LEAGUE_KEYS / LEAGUE_RULES sees it:
Pinnacle reference (OddsPapi tournament id or Odds API key), SportyBet league matching, priors, live format.
Built-in leagues can never be changed or removed from here. Pure logic; persistence is injected."""
import json
import re
import shlex
import statistics

from config.leagues import LEAGUES, LEAGUE_KEYS
from services.logger import get_logger

log = get_logger("leagues_dyn")
KEY = "leagues_extra"
BUILTIN = set(LEAGUES)           # captured at import: these are never touched
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,11}$")

USAGE = ("Usage: /addleague <code> \"League Name\" tid=<OddsPapi tournament id> [odds=<Odds API key>]\n"
         "  [match=greece+a1] [not=youth,reserve] [total=165] [margin=11.5] [home=3.5] [fmt=4x10]\n"
         "code: 2-12 letters/digits/_ (e.g. gre). match: words that must all appear in SportyBet's 'country + league' "
         "text (default: the league name's words). fmt = periods x minutes (default 4x10).\n"
         "Missing total / margin / home are estimated from Pinnacle's current lines when OddsPapi has games for the tid.\n"
         "/probe <tid> checks a tournament id first. /removeleague <code> removes an added league.")


def _load_raw(kv_get) -> dict:
    try:
        raw = kv_get(KEY)
        d = json.loads(raw) if raw else {}
    except Exception as e:
        log.warning("added leagues unreadable (%s)", e)
        d = {}
    return d if isinstance(d, dict) else {}


def apply(code: str, cfg: dict) -> None:
    """Merge one stored league into the live config (in place)."""
    from core.bookmakers._base import LEAGUE_RULES
    from core import live_model
    if code in BUILTIN:
        return
    entry = dict(name=cfg.get("name") or code.upper(), odds_api_key=cfg.get("odds_api_key") or None,
                 avg_total=float(cfg.get("avg_total") or 160.0), std_margin=float(cfg.get("std_margin") or 11.5),
                 home_adv=float(cfg.get("home_adv") if cfg.get("home_adv") is not None else 3.0), added=True)
    if cfg.get("oddspapi_tid"):
        entry["oddspapi_tid"] = int(cfg["oddspapi_tid"])
    LEAGUES[code] = entry
    if code not in LEAGUE_KEYS:
        LEAGUE_KEYS.append(code)
    inc = cfg.get("match") or [[w] for w in []]
    inc = [list(c) for c in inc] if inc else [re.findall(r"[a-z0-9.]+", entry["name"].lower())]
    LEAGUE_RULES[code] = (inc, [str(x).lower() for x in cfg.get("exclude") or []])
    if cfg.get("fmt"):
        n, mins = cfg["fmt"]
        live_model.FORMATS[code] = (int(n), int(mins) * 60)


def remove_live(code: str) -> None:
    from core.bookmakers._base import LEAGUE_RULES
    from core import live_model
    LEAGUES.pop(code, None)
    if code in LEAGUE_KEYS:
        LEAGUE_KEYS.remove(code)
    LEAGUE_RULES.pop(code, None)
    live_model.FORMATS.pop(code, None)


def load(kv_get) -> list:
    """Apply every stored league. Returns the codes applied."""
    done = []
    for code, cfg in _load_raw(kv_get).items():
        try:
            apply(code, cfg)
            done.append(code)
        except Exception as e:
            log.warning("added league %s skipped: %s", code, e)
    if done:
        log.info("added leagues loaded: %s", done)
    return done


def parse(arg: str) -> dict:
    """'gre "Greece A1" tid=250 total=160 match=greece+a1 not=youth' -> dict, or raises ValueError(message)."""
    try:
        toks = shlex.split(arg.replace("“", '"').replace("”", '"'))
    except ValueError:
        raise ValueError("Unbalanced quotes.\n" + USAGE)
    if len(toks) < 2:
        raise ValueError(USAGE)
    code, rest = toks[0].lower(), toks[1:]
    if not CODE_RE.match(code):
        raise ValueError("Bad code '%s': 2-12 characters, start with a letter, only a-z 0-9 _." % code)
    name_parts, kv = [], {}
    for t in rest:
        if "=" in t and t.split("=", 1)[0].lower() in ("tid", "odds", "match", "not", "total", "margin", "home", "fmt"):
            k, v = t.split("=", 1)
            kv[k.lower()] = v.strip()
        else:
            name_parts.append(t)
    out = dict(code=code, name=" ".join(name_parts).strip())
    if not out["name"]:
        raise ValueError("Give the league a name in quotes.\n" + USAGE)
    try:
        if "tid" in kv:
            out["oddspapi_tid"] = int(kv["tid"])
        for k, f in (("total", "avg_total"), ("margin", "std_margin"), ("home", "home_adv")):
            if k in kv:
                out[f] = float(kv[k])
        if "fmt" in kv:
            n, m = kv["fmt"].lower().split("x")
            n, m = int(n), int(m)
            if n not in (2, 4) or not 5 <= m <= 24:
                raise ValueError
            out["fmt"] = [n, m]
    except ValueError:
        raise ValueError("A number is not valid (tid, total, margin, home, fmt like 4x10).\n" + USAGE)
    if kv.get("odds"):
        out["odds_api_key"] = kv["odds"]
    if kv.get("match"):
        out["match"] = [[w for w in re.split(r"[+&]", kv["match"].lower()) if w]]
    if kv.get("not"):
        out["exclude"] = [w for w in re.split(r"[,]", kv["not"].lower()) if w.strip()]
    if not out.get("oddspapi_tid") and not out.get("odds_api_key"):
        raise ValueError("Needs a Pinnacle source: tid=<OddsPapi tournament id> and/or odds=<Odds API key>.\n" + USAGE)
    for f in ("avg_total", "std_margin", "home_adv"):
        if f in out and out[f] <= (-20 if f == "home_adv" else 0):
            raise ValueError("%s must be positive." % f)
    return out


def estimate_priors(events: list) -> dict:
    """Pinnacle's current main lines of a probe -> prior total / margin / home edge. {} when not enough games."""
    from core.positions import main_line
    tot, spr = [], []
    for e in events:
        t = main_line(e, "totals")
        s = main_line(e, "spreads")
        if t:
            tot.append(t)
        if s is not None:
            spr.append(s)
    out = {}
    if len(tot) >= 3:
        out["avg_total"] = round(statistics.median(tot), 1)
    if len(spr) >= 3:
        out["std_margin"] = round(max(8.0, min(16.0, 0.0 + statistics.median(abs(x) for x in spr) * 0.5 + 9.5)), 1)
        out["home_adv"] = round(max(1.5, min(5.0, -statistics.median(spr) if abs(statistics.median(spr)) < 5 else 3.0)), 1)
    return out


def probe_text(tid, raw, events, stats, kinds_seen=None) -> str:
    """Plain-text report of a /probe."""
    if not raw:
        return ("tid %s: OddsPapi returned no fixtures. Either the id is wrong or the league has no upcoming games "
                "right now (off-season). Nothing was added." % tid)
    names = sorted({str(f.get("tournamentName")) for f in raw if f.get("tournamentName")})[:3]
    lines = ["tid %s: %d fixtures, %d with Pinnacle prices%s" % (tid, len(raw), len(events), " (%s)" % ", ".join(names) if names else "")]
    for e in events[:4]:
        c = e.get("commence")
        lines.append("  %s vs %s  %s" % (e.get("home"), e.get("away"), c.strftime("%d %b %H:%M") if hasattr(c, "strftime") else (c or "")[:16]))
    mk = sorted({m for e in events for m in (e.get("markets") or {})})
    lines.append("markets seen: " + (", ".join(mk) or "none"))
    if not events:
        lines.append("Fixtures exist but none carry Pinnacle prices yet; the league can be added, edges will appear once priced.")
    pri = estimate_priors(events)
    if pri:
        lines.append("estimated priors: total %s, margin sd %s, home edge %s" % (
            pri.get("avg_total", "?"), pri.get("std_margin", "?"), pri.get("home_adv", "?")))
    nn = (stats or {}).get("no_names")
    if nn:
        lines.append("%d fixture(s) skipped: team names unknown to OddsPapi" % nn)
    return "\n".join(lines)


def save_one(kv_get, kv_set, cfg: dict) -> None:
    d = _load_raw(kv_get)
    d[cfg["code"]] = {k: v for k, v in cfg.items() if k != "code"}
    kv_set(KEY, json.dumps(d))


def delete_one(kv_get, kv_set, code: str) -> bool:
    d = _load_raw(kv_get)
    if code not in d:
        return False
    del d[code]
    kv_set(KEY, json.dumps(d))
    return True


def find_tournaments(rows: list, query: str, limit: int = 15) -> str:
    """Plain-text answer of /tournaments <words>. Every word must match a whole word of 'country + name', or be at
    least 3 letters and start one (so 'g' finds 'G League' but not 'Angola'). Busiest tournaments first."""
    words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if w]

    def hit(w, tokens):
        return w in tokens or (len(w) >= 3 and any(t.startswith(w) for t in tokens))

    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        tid = r.get("tournamentId", r.get("id"))
        name = r.get("tournamentName", r.get("name")) or ""
        cat = r.get("categoryName", r.get("category")) or ""
        if tid is None or not name:
            continue
        tokens = re.findall(r"[a-z0-9]+", ("%s %s" % (cat, name)).lower())
        if all(hit(w, tokens) for w in words):
            parts = [r.get(k) for k in ("liveFixtures", "upcomingFixtures", "futureFixtures")]   # OddsPapi docs: live / next 24h / later
            parts = [v for v in parts if isinstance(v, (int, float)) and not isinstance(v, bool)]
            out.append((str(cat), str(name), tid, sum(parts) if parts else None))
    if not out:
        return "No OddsPapi basketball tournament matches '%s'." % (query or "").strip()
    out.sort(key=lambda x: (-(x[3] or 0), x[0], x[1]))
    lines = ["%d match(es)%s (most games first):" % (len(out), "" if len(out) <= limit else ", first %d" % limit)]
    for cat, name, tid, n in out[:limit]:
        lines.append("%s  %s%s  tid=%s%s" % (cat, "- " if cat else "", name, tid, "  (%s fixtures)" % int(n) if n is not None else ""))
    lines.append("Then: /probe <tid> to see games and prices, /addleague to add it.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# /linkall: built-in leagues that have no Pinnacle source yet get one from OddsPapi's own tournament list, but ONLY
# when the match is unambiguous (exactly one tournament fits the rule) AND a live probe shows games with Pinnacle
# prices. Anything else is reported, never guessed. A link is stored in the kv table (key 'league_links') and never
# overrides a league that already has a source.
LINKS_KEY = "league_links"
LINK_RULES = {
    # code: (country words, [any-of word groups], excluded words)
    "g_league": (["usa"], [["g", "league"]], ["cup", "showcase", "women", "summer", "u19", "youth", "tip", "tournament"]),
    "bsl": (["turkey", "turkiye"], [["bsl"], ["super", "ligi"], ["super", "lig"], ["super", "league"]],
            ["women", "kadinlar", "cup", "kupasi", "u19", "u21", "youth", "tbl"]),
    # from 2026-27 the top division B1 is called "B.League Premier" (B2 -> "One", B3 -> "Next"): checked on Wikipedia
    "japan_b1": (["japan"], [["b1"], ["premier"]],
                 ["b2", "b3", "one", "next", "2", "3", "w", "women", "cup", "u18", "u22", "star", "all", "youth"]),
    "bal": ([], [["africa", "league"]], ["women", "qualifiers", "qualifier", "qualification", "u18"]),
}


def _tokens(*parts):
    return re.findall(r"[a-z0-9]+", " ".join(str(p or "") for p in parts).lower())


def _word_hit(w, tokens):
    return w in tokens or (len(w) >= 3 and any(t.startswith(w) for t in tokens))


def link_candidates(rows: list, code: str) -> list:
    """Tournaments of OddsPapi's list that fit LINK_RULES[code]: [(tid, category, name, fixtures)]."""
    country, any_of, exclude = LINK_RULES[code]
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        tid = r.get("tournamentId", r.get("id"))
        name, cat = r.get("tournamentName", r.get("name")) or "", r.get("categoryName", r.get("category")) or ""
        if tid is None or not name:
            continue
        ct, nt = _tokens(cat), _tokens(name)
        toks = ct + nt
        if country and not any(_word_hit(c, ct) for c in country):
            continue
        if not any(all(_word_hit(w, toks) for w in grp) for grp in any_of):
            continue
        if any(x in toks for x in exclude):
            continue
        parts = [r.get(k) for k in ("liveFixtures", "upcomingFixtures", "futureFixtures")]
        parts = [v for v in parts if isinstance(v, (int, float)) and not isinstance(v, bool)]
        out.append((int(tid), str(cat), str(name), int(sum(parts)) if parts else 0))
    return out


def load_links(kv_get) -> dict:
    try:
        raw = kv_get(LINKS_KEY)
        d = json.loads(raw) if raw else {}
    except Exception:
        d = {}
    return d if isinstance(d, dict) else {}


def apply_link(code: str, tid: int) -> bool:
    """Give a built-in league WITHOUT a Pinnacle source an OddsPapi tournament id. False when it already has a source."""
    cfg = LEAGUES.get(code)
    if not cfg or cfg.get("odds_api_key") or cfg.get("oddspapi_tid"):
        return False
    cfg["oddspapi_tid"] = int(tid)
    cfg["linked"] = True
    return True


def load_all_links(kv_get) -> list:
    done = [c for c, t in load_links(kv_get).items() if isinstance(t, int) and apply_link(c, t)]
    if done:
        log.info("league links loaded: %s", done)
    return done


def save_link(kv_get, kv_set, code: str, tid: int) -> None:
    d = load_links(kv_get)
    d[code] = int(tid)
    kv_set(LINKS_KEY, json.dumps(d))


def linkall(rows: list, probe, kv_get, kv_set) -> str:
    """Run the linking for every built-in league that has no source. probe(tid) -> (raw, events, stats)."""
    lines = []
    for code in LINK_RULES:
        cfg = LEAGUES.get(code)
        if cfg is None:
            continue
        if cfg.get("odds_api_key") or cfg.get("oddspapi_tid"):
            lines.append("%s: already has a Pinnacle source (tid %s%s)" % (code, cfg.get("oddspapi_tid", "-"), ", Odds API" if cfg.get("odds_api_key") else ""))
            continue
        cands = link_candidates(rows, code)
        if len(cands) != 1:
            if not cands:
                lines.append("%s: NOT LINKED - no OddsPapi tournament fits (not offered, or named differently)" % code)
            else:
                lines.append("%s: NOT LINKED - %d tournaments fit, refusing to guess: %s" % (
                    code, len(cands), "; ".join("%s %s tid=%s (%s games)" % (c, n, t, f) for t, c, n, f in cands[:5])))
            continue
        tid, cat, name, fx = cands[0]
        if fx <= 0:
            lines.append("%s: NOT LINKED - found %s %s tid=%s but it has no games right now (off-season); run /linkall again later" % (code, cat, name, tid))
            continue
        try:
            _raw, events, _st = probe(tid)
        except Exception as e:
            lines.append("%s: NOT LINKED - probe of tid %s failed: %s" % (code, tid, e))
            continue
        if not events:
            lines.append("%s: NOT LINKED - tid %s (%s %s) has games but no Pinnacle prices yet" % (code, tid, cat, name))
            continue
        apply_link(code, tid)
        save_link(kv_get, kv_set, code, tid)
        mk = sorted({m for e in events for m in (e.get("markets") or {})})
        lines.append("%s: LINKED to %s %s tid=%s - %d game(s) with Pinnacle prices; markets: %s" % (code, cat, name, tid, len(events), ", ".join(mk)))
    return "\n".join(lines) or "Nothing to link."
