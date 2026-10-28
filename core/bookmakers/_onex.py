# core/bookmakers/_onex.py
"""1xBet-platform feed (shared by 1xBet and Paripesa). Basketball = sport 3.
Groups: G1 1x2 (T1 home, T3 away) | G2 handicap (T7 home, T8 away, P=line) | G17 total (T9 over, T10 under, P=line)."""
from typing import Dict, List, Optional

from core.bookmakers._base import (build_rows, fnum, get_json, log_shape_once, most_balanced,
                                   pair_candidates, save_capture, to_iso, wanted)


def _flatten(ev: Dict) -> List[Dict]:
    out = [x for x in (ev.get("E") or []) if isinstance(x, dict)]
    for g in ev.get("GE") or []:
        for col in g.get("E") or []:
            out.extend(x for x in (col if isinstance(col, list) else [col]) if isinstance(x, dict))
    return out


def parse_value(items: List[Dict], book: str, league_id: Optional[str]) -> List[Dict]:
    rows: List[Dict] = []
    for ev in items or []:
        home, away = ev.get("O1"), ev.get("O2")
        league = wanted(f'{ev.get("L", "")} {ev.get("LE", "")}', league_id)
        if not league or not home or not away:
            continue
        odds = _flatten(ev)
        pick = lambda g, t: [(fnum(o.get("P")) or 0.0, fnum(o.get("C"))) for o in odds
                             if o.get("G") == g and o.get("T") == t and fnum(o.get("C"))]
        ml_h = next((fnum(o.get("C")) for o in odds if o.get("G") == 1 and o.get("T") == 1), None)
        ml_a = next((fnum(o.get("C")) for o in odds if o.get("G") == 1 and o.get("T") == 3), None)
        sp = most_balanced(pair_candidates(pick(2, 7), pick(2, 8), negate=True))
        tt = most_balanced(pair_candidates(pick(17, 9), pick(17, 10)))
        rows.extend(build_rows(book, league, ev.get("I"), home, away, to_iso(ev.get("S")),
                               ml=(ml_h, ml_a) if ml_h and ml_a else None,
                               spread=(sp[0], sp[1], sp[2]) if sp else None,
                               total=(tt[0], tt[1], tt[2]) if tt else None))
    return rows


async def fetch(book: str, host: str, league_id: Optional[str]) -> List[Dict]:
    payload = await get_json(host.rstrip("/") + "/LineFeed/Get1x2_VZip",
                             params={"sports": 3, "count": 200, "lng": "en", "mode": 4, "getEmpty": "true"})
    if not payload:
        return []
    log_shape_once(book, payload)
    save_capture(book, payload)
    return parse_value(payload.get("Value") or [], book, league_id)
