"""Player / tournament name handling for tennis. The three feeds write the same player differently:
'Djokovic, Novak' (SportyBet) / 'Novak Djokovic' (Pinnacle) / 'Djokovic N.' (Sofascore). A player is reduced to
(surname tokens, first initial); two players are the same only when the surname agrees AND the initials do not clash.
Doubles ('A / B') are matched pair by pair. Home / away order differs between books, so pairs are matched in BOTH
orders and the caller gets to know whether the order was swapped."""
from __future__ import annotations

import difflib
import re
import unicodedata

_PARTICLES = {"de", "del", "della", "di", "da", "van", "von", "der", "den", "ten", "ter", "la", "le", "el", "al", "bin", "dos", "do", "du"}
BAD_LEAGUE = ("virtual", "esoccer", "e-soccer", "cyber", "srl", "simulated", "esports", "e-sports", "exhibition", "u16", "u17", "u18",
              "u19", "u20", "junior", "juniors", "boys", "girls", "youth", "table tennis", "beach")


def ascii_(s: str) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()


def tokens(name: str) -> list:
    s = ascii_(name).lower().replace("&", " and ").replace("'", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return [t for t in s.split() if t]


def player(name: str):
    """-> (surname tokens tuple, given-name or initial string or '')."""
    raw = ascii_(name).strip()
    if "," in raw:
        sur, _, giv = raw.partition(",")
        st, gt = tokens(sur), tokens(giv)
        return tuple(st), (gt[0] if gt else "")
    toks = tokens(raw)
    if not toks:
        return (), ""
    if len(toks) >= 2 and len(toks[-1]) == 1:                       # 'Djokovic N.'
        return tuple(toks[:-1]), toks[-1]
    if len(toks) >= 2 and len(toks[0]) == 1 and len(toks[1]) > 1:   # 'N. Djokovic'
        return tuple(toks[1:]), toks[0]
    if len(toks) == 1:
        return (toks[0],), ""
    return tuple(toks[1:]), toks[0]                                 # 'Novak Djokovic' / 'Alex de Minaur'


def _sur_sim(a: tuple, b: tuple) -> float:
    sa = " ".join(t for t in a if t not in _PARTICLES) or " ".join(a)
    sb = " ".join(t for t in b if t not in _PARTICLES) or " ".join(b)
    if not sa or not sb:
        return 0.0
    if sa == sb:
        return 1.0
    ta, tb = set(sa.split()), set(sb.split())
    if ta and tb and (ta <= tb or tb <= ta) and len(ta & tb) >= 1 and max(len(x) for x in ta & tb) >= 4:
        return 0.93
    return difflib.SequenceMatcher(None, sa, sb).ratio()


def _given_ok(ga: str, gb: str) -> bool:
    if not ga or not gb:
        return True
    if ga[0] != gb[0]:
        return False
    if len(ga) > 1 and len(gb) > 1 and len(ga) >= 3 and len(gb) >= 3:
        return difflib.SequenceMatcher(None, ga, gb).ratio() >= 0.6
    return True


def split_pair(name: str) -> list:
    parts = [p for p in re.split(r"\s*(?:/|\band\b|\+)\s*", str(name or ""), flags=re.I) if p.strip()]
    return parts if len(parts) == 2 else [str(name or "")]


def one_score(a: str, b: str) -> float:
    (sa, ga), (sb, gb) = player(a), player(b)
    if not _given_ok(ga, gb):
        return 0.0
    s = _sur_sim(sa, sb)
    return s if s >= 0.84 else 0.0


def same_side(a: str, b: str) -> float:
    """0 when two entries are not the same player / doubles pair, else a 0..1 match strength."""
    pa, pb = split_pair(a), split_pair(b)
    if len(pa) != len(pb):
        return 0.0
    if len(pa) == 1:
        return one_score(a, b)
    best = 0.0
    for q in (pb, pb[::-1]):
        s = [one_score(x, y) for x, y in zip(pa, q)]
        if all(s):
            best = max(best, sum(s) / 2)
    return best


def pair_score(h1, a1, h2, a2):
    """-> (score, swapped). 'swapped' = the second fixture lists the players in the opposite order. Score 0 = different game."""
    s1 = (same_side(h1, h2), same_side(a1, a2))
    s2 = (same_side(h1, a2), same_side(a1, h2))
    f = sum(s1) / 2 if all(s1) else 0.0
    g = sum(s2) / 2 if all(s2) else 0.0
    if f == 0.0 and g == 0.0:
        return 0.0, False
    return (f, False) if f >= g else (g, True)


def similarity(a: str, b: str) -> float:
    ta, tb = " ".join(tokens(a)), " ".join(tokens(b))
    return difflib.SequenceMatcher(None, ta, tb).ratio() if ta and tb else 0.0


def bad_league(text: str) -> bool:
    t = " " + " ".join(tokens(text)) + " "
    return any((" " + w + " ") in t for w in BAD_LEAGUE)


def league_key(country: str, name: str) -> str:
    return "%s|%s" % (" ".join(tokens(country)), " ".join(tokens(name)))
