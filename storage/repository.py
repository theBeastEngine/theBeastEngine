"""All DB access. There is deliberately NO delete function for the gold table."""
import json
import os
from datetime import timedelta
from sqlalchemy import select, func, and_, or_
from config.settings import settings, PINNACLE
from storage.db import get_session, get_engine
from storage.models import OddsReferenceGold, Pick, BankrollLog, ApiUsage, KvStore, PinHistory
from utils.helpers import utcnow


def sqlite_insert(model):
    """INSERT ... ON CONFLICT builder for whichever database is active (SQLite locally, Postgres on Neon).
    Name kept so existing call sites read the same."""
    if get_engine().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model)


GOLD_COLS = ["league", "matchup_type", "market", "source", "line", "side_a_odds", "side_b_odds", "is_derived"]
_KEY_COLS = ("league", "matchup_type", "market", "source")
_CHUNK = 500   # 9 bound params per row: keeps every statement far below SQLite's variable limit


def _d(o):
    return {c.name: getattr(o, c.name) for c in o.__table__.columns} if o is not None else None


def _today_start():
    return utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------- gold ----------------
def upsert_gold(rows: list, protect_real: bool = False) -> int:
    """Insert/update gold rows. A REAL row (is_derived False) is never overwritten by a DERIVED one,
    whatever the caller passes. protect_real=True additionally makes derived rows the only ones that
    can be overwritten at all (kept for the derive fill)."""
    if not rows:
        return 0
    clean = []
    for r in rows:
        c = {k: r.get(k) for k in GOLD_COLS}
        if any(not c[k] for k in _KEY_COLS):
            raise ValueError(f"gold row missing key column: {r}")
        c["is_derived"] = bool(c.get("is_derived"))
        c["updated_at"] = utcnow()
        clean.append(c)
    n = 0
    with get_session() as s:
        for i in range(0, len(clean), _CHUNK):
            chunk = clean[i:i + _CHUNK]
            stmt = sqlite_insert(OddsReferenceGold).values(chunk)
            if protect_real:
                guard = OddsReferenceGold.is_derived.is_(True)
            else:
                guard = or_(OddsReferenceGold.is_derived.is_(True), stmt.excluded.is_derived.is_(False))
            stmt = stmt.on_conflict_do_update(
                index_elements=list(_KEY_COLS),
                set_={k: stmt.excluded[k] for k in ("line", "side_a_odds", "side_b_odds", "is_derived", "updated_at")},
                where=guard,
            )
            s.execute(stmt)
            n += len(chunk)
    return n


def gold_count(source: str = None, league: str = None) -> int:
    with get_session() as s:
        q = select(func.count()).select_from(OddsReferenceGold)
        if source:
            q = q.where(OddsReferenceGold.source == source)
        if league:
            q = q.where(OddsReferenceGold.league == league)
        return s.execute(q).scalar_one()


def gold_get(league: str, matchup_type: str, market: str, source: str = PINNACLE):
    with get_session() as s:
        q = select(OddsReferenceGold).where(and_(
            OddsReferenceGold.league == league, OddsReferenceGold.matchup_type == matchup_type,
            OddsReferenceGold.market == market, OddsReferenceGold.source == source))
        return _d(s.execute(q).scalar_one_or_none())


def gold_rows(source: str = None, league: str = None) -> list:
    """Bulk read: load once per scan and look up in a dict instead of calling gold_get per market."""
    with get_session() as s:
        q = select(OddsReferenceGold)
        if source:
            q = q.where(OddsReferenceGold.source == source)
        if league:
            q = q.where(OddsReferenceGold.league == league)
        return [_d(r) for r in s.execute(q).scalars()]


def backup_pinnacle(path: str = None) -> int:
    """Atomic backup. NEVER writes an empty backup over a good one (a wiped DB must not wipe the backup);
    keeps the previous good copy as <path>.prev."""
    path = path or settings.GOLD_BACKUP_PATH
    rows = gold_rows(source=PINNACLE)
    if not rows:
        return 0
    for r in rows:
        r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else None
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f)
    if os.path.exists(path):
        os.replace(path, path + ".prev")
    os.replace(tmp, path)  # atomic: an interrupted backup never corrupts the old one
    return len(rows)


def restore_pinnacle(path: str = None) -> int:
    """Restore only rows that are MISSING from the DB; existing rows (possibly fresher) are untouched."""
    path = path or settings.GOLD_BACKUP_PATH
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        rows = json.load(f)
    if not rows:
        return 0
    n = 0
    with get_session() as s:
        for i in range(0, len(rows), _CHUNK):
            chunk = []
            for r in rows[i:i + _CHUNK]:
                c = {k: r.get(k) for k in GOLD_COLS}
                c["is_derived"] = bool(c.get("is_derived"))
                c["updated_at"] = utcnow()
                chunk.append(c)
            stmt = sqlite_insert(OddsReferenceGold).values(chunk).on_conflict_do_nothing(index_elements=list(_KEY_COLS))
            n += s.execute(stmt).rowcount or 0
    return n


# ---------------- picks ----------------
def add_pick(**kw) -> int:
    with get_session() as s:
        p = Pick(**kw)
        s.add(p)
        s.flush()
        return p.id


def recently_sent(league, event_id, market, selection, minutes: int = 30) -> bool:
    cutoff = utcnow() - timedelta(minutes=minutes)
    with get_session() as s:
        q = select(func.count()).select_from(Pick).where(and_(
            Pick.league == league, Pick.event_id == event_id, Pick.market == market,
            Pick.selection == selection, Pick.sent_at.is_not(None), Pick.sent_at >= cutoff))
        return s.execute(q).scalar_one() > 0


def live_history(league, event_id, minutes: int = 360) -> list:
    """Live alerts already created for this game in the last `minutes` (newest first)."""
    cutoff = utcnow() - timedelta(minutes=minutes)
    with get_session() as s:
        q = (select(Pick).where(and_(Pick.league == league, Pick.event_id == event_id, Pick.mode == "live",
                                     Pick.created_at >= cutoff)).order_by(Pick.created_at.desc()))
        return [_d(p) for p in s.execute(q).scalars()]


def prematch_picks_for(league, home, away, hours: int = 72) -> list:
    """Pre-match alerts sent for one game (any status). Names are compared by the caller (feeds spell teams differently)."""
    cutoff = utcnow() - timedelta(hours=hours)
    with get_session() as s:
        q = select(Pick).where(and_(Pick.league == league, Pick.mode == "prematch", Pick.created_at >= cutoff))
        return [_d(p) for p in s.execute(q).scalars()]


def mark_sent(pick_id: int) -> None:
    with get_session() as s:
        p = s.get(Pick, pick_id)
        if p:
            p.sent_at = utcnow()


def get_pick(pick_id: int):
    with get_session() as s:
        return _d(s.get(Pick, pick_id))


def set_status(pick_id: int, status: str) -> None:
    with get_session() as s:
        p = s.get(Pick, pick_id)
        if p and p.status not in ("won", "lost", "void"):   # a settled pick is final
            p.status = status


def placed_picks_for_event(league, event_id) -> list:
    """Placed (not yet settled) picks of one game: what /score settles and what the per-match exposure cap counts."""
    with get_session() as s:
        q = select(Pick).where(and_(Pick.league == league, Pick.event_id == str(event_id), Pick.status == "placed"))
        return [_d(p) for p in s.execute(q).scalars()]


def open_exposure(league, event_id) -> float:
    """Money already placed on this game and not yet settled."""
    try:
        return float(sum(p.get("stake") or 0 for p in placed_picks_for_event(league, event_id)))
    except Exception:
        return 0.0


def set_took_odds(pick_id: int, odds: float) -> None:
    """Log the price actually taken on the Nigerian book (/took, /odds <x>)."""
    odds = float(odds)
    if odds <= 1.0:
        raise ValueError("odds must be > 1.0")
    with get_session() as s:
        p = s.get(Pick, pick_id)
        if p and p.status not in ("won", "lost", "void"):
            p.took_odds = odds
            p.status = "placed"


def picks_today(mode: str = None) -> list:
    with get_session() as s:
        q = select(Pick).where(Pick.created_at >= _today_start()).order_by(Pick.ev.desc())
        if mode:
            q = q.where(Pick.mode == mode)
        return [_d(p) for p in s.execute(q).scalars()]


def placed_bets(limit: int = 20) -> list:
    with get_session() as s:
        q = (select(Pick).where(Pick.status.in_(("placed", "won", "lost", "void")))
             .order_by(Pick.created_at.desc()).limit(limit))
        return [_d(p) for p in s.execute(q).scalars()]


def all_picks(limit: int = 200) -> list:
    """Newest first. Used by /export (the Free plan wipes the database on every deploy, so save a copy first)."""
    with get_session() as s:
        q = select(Pick).order_by(Pick.id.desc()).limit(int(limit))
        return [_d(p) for p in s.execute(q).scalars()]


def settled_picks(mode: str = None, limit: int = 5000) -> list:
    """Won / lost picks, oldest first (the /calib report)."""
    with get_session() as s:
        q = select(Pick).where(Pick.status.in_(("won", "lost"))).order_by(Pick.id.asc()).limit(int(limit))
        if mode:
            q = q.where(Pick.mode == mode)
        return [_d(p) for p in s.execute(q).scalars()]


def add_pin_history(rows: list) -> int:
    """Pinnacle price history rows (dicts with ts, league, event_id, home, away, commence, snap)."""
    if not rows:
        return 0
    with get_session() as s:
        for i in range(0, len(rows), _CHUNK):
            s.add_all([PinHistory(**r) for r in rows[i:i + _CHUNK]])
    return len(rows)


def pin_history_between(league: str, start, end) -> list:
    """History rows of one league for games starting in [start, end)."""
    with get_session() as s:
        q = (select(PinHistory).where(and_(PinHistory.league == league, PinHistory.commence >= start,
                                           PinHistory.commence < end)).order_by(PinHistory.ts.asc()))
        return [_d(r) for r in s.execute(q).scalars()]


def bets_today_on_book(book: str) -> int:
    with get_session() as s:
        q = select(func.count()).select_from(Pick).where(and_(
            Pick.book == book, Pick.status == "placed", Pick.created_at >= _today_start()))
        return s.execute(q).scalar_one()


# ---------------- bankroll ----------------
def _balance(s) -> float:
    row = s.execute(select(BankrollLog).order_by(BankrollLog.id.desc()).limit(1)).scalar_one_or_none()
    return row.balance if row else float(settings.BANKROLL_START)


def bankroll_current() -> float:
    with get_session() as s:
        return _balance(s)


def bankroll_set(balance: float, note: str = "") -> None:
    with get_session() as s:
        s.add(BankrollLog(balance=float(balance), note=note[:120]))


def settle_pick(pick_id: int, result: str) -> float:
    """result: won | lost | void. Only a PLACED pick moves the bankroll (skipped/new/sim picks never risked money).
    Uses the price actually taken. Pick status and bankroll are written in ONE transaction. Returns new bankroll."""
    if result not in ("won", "lost", "void"):
        raise ValueError("result must be won|lost|void")
    with get_session() as s:
        p = s.get(Pick, pick_id)
        if not p or p.status != "placed":
            return _balance(s)
        odds = p.took_odds or p.book_odds or p.min_odds
        profit = p.stake * (odds - 1) if result == "won" else (-p.stake if result == "lost" else 0.0)
        new = _balance(s) + profit
        p.status = result
        p.settled_at = utcnow()
        s.add(BankrollLog(balance=float(new), note=f"pick {pick_id} {result}"[:120]))
        return new


# ---------------- api credits ----------------
def log_api_usage(key_tail: str, endpoint: str, credits: int = 1) -> None:
    with get_session() as s:
        s.add(ApiUsage(key_tail=key_tail[-4:], endpoint=endpoint[:64], credits=credits))


def credits_used_today() -> int:
    with get_session() as s:
        return s.execute(select(func.coalesce(func.sum(ApiUsage.credits), 0)).where(ApiUsage.ts >= _today_start())).scalar_one()


def picks_between(league: str, start, end) -> list:
    """Picks for one league whose GAME falls in [start, end) (falls back to created_at when commence is unknown),
    best EV first."""
    with get_session() as s:
        when = or_(and_(Pick.commence.is_not(None), Pick.commence >= start, Pick.commence < end),
                   and_(Pick.commence.is_(None), Pick.created_at >= start, Pick.created_at < end))
        q = select(Pick).where(and_(Pick.league == league, when)).order_by(Pick.ev.desc())
        return [_d(p) for p in s.execute(q).scalars()]


# ---------------- key/value blobs (survive deploys when Neon is active) ----------------
def kv_get(key: str):
    with get_session() as s:
        row = s.get(KvStore, key)
        return row.value if row else None


def kv_set(key: str, value: str) -> None:
    with get_session() as s:
        stmt = sqlite_insert(KvStore).values(key=key, value=value, updated_at=utcnow())
        stmt = stmt.on_conflict_do_update(index_elements=["key"],
                                          set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at})
        s.execute(stmt)


def last_alert_ago():
    """Seconds since the last alert was delivered (None when there never was one)."""
    with get_session() as s:
        t = s.execute(select(func.max(Pick.sent_at))).scalar()
    if t is None:
        return None
    return max(0.0, (utcnow() - t).total_seconds())


def settled_between(start, end) -> list:
    """Picks settled in [start, end) (naive UTC datetimes): the daily wrap-up."""
    with get_session() as s:
        q = select(Pick).where(and_(Pick.settled_at.is_not(None), Pick.settled_at >= start, Pick.settled_at < end))
        return [_d(p) for p in s.execute(q).scalars()]


def created_between(start, end) -> list:
    with get_session() as s:
        q = select(Pick).where(and_(Pick.created_at >= start, Pick.created_at < end))
        return [_d(p) for p in s.execute(q).scalars()]
