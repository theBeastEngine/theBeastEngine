"""Team / league name handling. Handball has many near-identical names ('Arsenal' / 'Arsenal W', 'Barcelona' / 'Barcelona B',
'Inter' / 'Inter U19'), so two events are the same game only when the names agree AND the qualifiers (women, youth,
reserve side) are identical on both sides."""
from __future__ import annotations

import difflib
import re
import unicodedata

_GENERIC = {"fc", "cf", "afc", "sc", "ac", "as", "ss", "us", "cd", "ud", "sd", "rc", "rcd", "fk", "sk", "nk", "bk", "if", "ik",
            "sv", "vfl", "vfb", "tsv", "fsv", "club", "calcio", "de", "the", "and", "of", "bsc", "fbc", "cfc", "afc"}
_ALIAS = {"utd": "united", "intl": "international", "munchen": "munich", "muenchen": "munich", "koln": "cologne",
          "saint": "st", "sankt": "st", "wanderers": "wand", "athletic": "ath", "atletico": "atl", "borussia": "bor",
          "monchengladbach": "gladbach", "internazionale": "inter", "sporting": "sporting", "ii": "ii", "2": "ii"}
_WOMEN = {"w", "women", "womens", "ladies", "femenino", "femenina", "feminin", "feminine", "frauen", "damen", "dames", "fem"}
_YOUTH = {"u16", "u17", "u18", "u19", "u20", "u21", "u22", "u23", "youth", "juniors", "junior", "academy"}
_RESERVE = {"ii", "iii", "b", "reserves", "reserve", "res"}
BAD_LEAGUE = ("women", "womens", "femenina", "femenino", "feminina", "feminino", "femenil", "feminine", "feminin", "femminile",
              "frauen", "damen", "dames", "virtual", "ehandball", "e-handball", "cyber", "srl", "simulated", "fifa", "esports",
              "u16", "u17", "u18", "u19", "u20", "u21", "u23", "youth", "reserve", "reserves", "juniors")


def ascii_(s: str) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()


def tokens(name: str) -> list:
    s = ascii_(name).lower().replace("&", " and ").replace("'", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t]


def markers(name: str) -> frozenset:
    out = set()
    for t in tokens(name):
        if t in _WOMEN:
            out.add("w")
        elif t in _YOUTH:
            out.add("y:" + t)
        elif t in _RESERVE and t != "b":
            out.add("r")
    toks = tokens(name)
    if len(toks) > 1 and toks[-1] == "b":
        out.add("r")
    if len(toks) > 1 and toks[-1] == "2":
        out.add("r")
    return frozenset(out)


def core_tokens(name: str) -> list:
    out = []
    for t in tokens(name):
        if t in _GENERIC or t in _WOMEN or t in _YOUTH or t in _RESERVE or t == "2":
            continue
        out.append(_ALIAS.get(t, t))
    return out or [t for t in tokens(name)]


def norm(name: str) -> str:
    return " ".join(core_tokens(name))


def _tok_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)):
        return True
    return len(a) >= 4 and len(b) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.86


def similarity(a: str, b: str) -> float:
    """0..1. Token-wise (so 'Man Utd' ~ 'Manchester United') combined with a plain string ratio."""
    ta, tb = core_tokens(a), core_tokens(b)
    if not ta or not tb:
        return 0.0
    used, hit = set(), 0
    for x in ta:
        for j, y in enumerate(tb):
            if j not in used and _tok_match(x, y):
                used.add(j)
                hit += 1
                break
    tok = hit / max(len(ta), len(tb))
    ratio = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return max(tok, ratio)


def same_team(a: str, b: str, min_sim: float = 0.72) -> bool:
    return markers(a) == markers(b) and similarity(a, b) >= min_sim


def pair_score(h1, a1, h2, a2) -> float:
    """Score of 'these two fixtures are the same game' (0 when a qualifier differs or a side is too different)."""
    if not (same_team(h1, h2) and same_team(a1, a2)):
        return 0.0
    return (similarity(h1, h2) + similarity(a1, a2)) / 2


def bad_league(text: str) -> bool:
    t = " " + " ".join(tokens(text)) + " "
    return any((" " + w + " ") in t for w in BAD_LEAGUE)


def league_key(country: str, name: str) -> str:
    return "%s|%s" % (" ".join(tokens(country)), " ".join(tokens(name)))
