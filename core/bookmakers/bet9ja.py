# core/bookmakers/bet9ja.py
"""Bet9ja feapi. Odds dict keys: S_12_1/2 (winner), S_AH@-3.5_1/2 (handicap), S_OU@165.5_O/U (total).
Needs BET9JA_GROUPS = JSON {"nba": "<GROUPID>", ...} taken from the site's network calls."""
import json
import os
import re
from typing import Dict, List, Optional

from core.bookmakers._base import (build_rows, fnum, get_json, log_shape_once, logger, most_balanced,
                                   pair_candidates, save_capture)

BOOK = "bet9ja"
URL = os.getenv("BET9JA_URL", "https://sports.bet9ja.com/desktop/feapi/PalimpsestAjax/GetEventsInGroupV2")
_ML = re.compile(r"^S_(?:12|ML)_([12])$")
_AH = re.compile(r"^S_AH@(-?\d+(?:\.\d+)?)_([12])$")
_OU = re.compile(r"^S_OU@(\d+(?:\.\d+)?)_([OU])$")


def _groups() -> Dict[str, str]:
    try:
        return json.loads(os.getenv("BET9JA_GROUPS", "{}"))
    except ValueError:
        return {}


def parse_events(payload: Dict, league_id: str, book: str = BOOK) -> List[Dict]:
    rows: List[Dict] = []
    for ev in ((payload or {}).get("D") or {}).get("E") or []:
        name = str(ev.get("DS") or "")
        if " - " not in name:
            continue
        home, away = [s.strip() for s in name.split(" - ", 1)]
        ml, ah1, ah2, ou_o, ou_u = {}, [], [], [], []
        for k, v in (ev.get("O") or {}).items():
            o = fnum(v)
            if not o:
                continue
            if (m := _ML.match(k)):
                ml[m.group(1)] = o
            elif (m := _AH.match(k)):
                (ah1 if m.group(2) == "1" else ah2).append((float(m.group(1)), o))
            elif (m := _OU.match(k)):
                (ou_o if m.group(2) == "O" else ou_u).append((float(m.group(1)), o))
        sp = most_balanced(pair_candidates(ah1, ah2, negate=True))
        tt = most_balanced(pair_candidates(ou_o, ou_u))
        rows.extend(build_rows(book, league_id, ev.get("ID"), home, away,
                               str(ev.get("STARTDATE") or "") or None,
                               ml=(ml["1"], ml["2"]) if len(ml) == 2 else None,
                               spread=sp, total=tt))
    return rows


async def fetch_odds(league_id=None, *args, **kwargs) -> List[Dict]:
    groups = _groups()
    ids = [league_id] if league_id else list(groups)
    rows: List[Dict] = []
    for lid in ids:
        gid = groups.get(lid)
        if not gid:
            logger.info("bet9ja: no group id for %s (set BET9JA_GROUPS)", lid)
            continue
        payload = await get_json(URL, params={"GROUPID": gid, "DISP": "0"},
                                 headers={"Referer": "https://sports.bet9ja.com/"})
        if payload:
            log_shape_once(BOOK, payload)
            save_capture(BOOK, payload)
            rows.extend(parse_events(payload, lid))
    return rows
