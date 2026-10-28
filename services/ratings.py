"""Team tiers (TOP/MID/BOT) from data/team_tiers.json. Unknown teams default to MID.
Accepts {league: {TOP:[teams], MID:[...], BOT:[...]}} or {league: {team: tier}}."""
import difflib
import json
import re
import unicodedata
from pathlib import Path

from services.logger import get_logger

log = get_logger("ratings")
_PATH = Path(__file__).resolve().parents[1] / "data" / "team_tiers.json"
_TIERS = ("TOP", "MID", "BOT")
_cache = None
_memo = {}


def _norm(s) -> str:
    """Lower-case, accents stripped ('Fenerbah\u00e7e' = 'fenerbahce'), hyphens as spaces, None-safe."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower().replace("-", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", s)).strip()


def _load():
    global _cache
    if _cache is None:
        _cache = {}
        _memo.clear()
        try:
            with open(_PATH) as f:
                raw = json.load(f)
        except Exception as e:
            # CLAUDE FIX: a missing/broken file silently made every team MID (all matchups MID_MID).
            log.warning("team tiers not loaded from %s (%s: %s): every team will be MID", _PATH, type(e).__name__, e)
            raw = {}
        for lg, v in raw.items():
            m = {}
            if isinstance(v, dict) and v and set(v) <= set(_TIERS):
                for tier, teams in v.items():
                    for t in teams:
                        m[_norm(t)] = tier
            elif isinstance(v, dict):
                for t, tier in v.items():
                    if str(tier).upper() in _TIERS:
                        m[_norm(t)] = str(tier).upper()
            _cache[lg] = m
    return _cache


def reload():
    global _cache
    _cache = None
    _memo.clear()


def tier_of(league: str, team: str) -> str:
    m = _load().get(league, {})
    n = _norm(team)
    key = (league, n)
    if key in _memo:
        return _memo[key]
    if n in m:
        tier = m[n]
    else:
        hit = difflib.get_close_matches(n, list(m), n=1, cutoff=0.85)
        tier = m[hit[0]] if hit else "MID"
    _memo[key] = tier
    return tier


def matchup_type(league: str, home: str, away: str) -> str:
    return f"{tier_of(league, home)}_{tier_of(league, away)}"
