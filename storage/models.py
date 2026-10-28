from typing import Optional
from datetime import datetime
from sqlalchemy import String, Float, Integer, DateTime, Boolean, Text, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from utils.helpers import utcnow


class Base(DeclarativeBase):
    pass


class OddsReferenceGold(Base):
    """Gold table: 14 leagues x 15 markets x 9 tiers per source. Never deleted."""
    __tablename__ = "odds_reference_gold"
    __table_args__ = (
        UniqueConstraint("league", "matchup_type", "market", "source", name="uq_gold"),
        Index("ix_gold_source", "source"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league: Mapped[str] = mapped_column(String(24))
    matchup_type: Mapped[str] = mapped_column(String(16))
    market: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    line: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    side_a_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # home / over
    side_b_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # away / under
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Pick(Base):
    __tablename__ = "picks"
    __table_args__ = (Index("ix_pick_dedupe", "league", "event_id", "market", "selection"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    mode: Mapped[str] = mapped_column(String(12))  # prematch | live | sim
    league: Mapped[str] = mapped_column(String(24))
    event_id: Mapped[str] = mapped_column(String(64))
    home: Mapped[str] = mapped_column(String(80))
    away: Mapped[str] = mapped_column(String(80))
    commence: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    market: Mapped[str] = mapped_column(String(32))
    selection: Mapped[str] = mapped_column(String(80))
    line: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fair_odds: Mapped[float] = mapped_column(Float)
    min_odds: Mapped[float] = mapped_column(Float)
    book: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    book_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ev: Mapped[float] = mapped_column(Float)
    stake: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10), default="new")  # new|placed|skipped|won|lost|void
    took_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class BankrollLog(Base):
    __tablename__ = "bankroll_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    balance: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(String(120), default="")


class ApiUsage(Base):
    __tablename__ = "api_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    key_tail: Mapped[str] = mapped_column(String(8), default="")
    endpoint: Mapped[str] = mapped_column(String(64), default="")
    credits: Mapped[int] = mapped_column(Integer, default=1)


class KvStore(Base):
    """Small key/value blobs that must survive a deploy (the Pinnacle price cache and its credit counter)."""
    __tablename__ = "kv_store"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PinHistory(Base):
    """One row per game whenever its Pinnacle main lines change (closing-line value and calibration later)."""
    __tablename__ = "pin_history"
    __table_args__ = (Index("ix_pinhist_league_commence", "league", "commence"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    league: Mapped[str] = mapped_column(String(24))
    event_id: Mapped[str] = mapped_column(String(64))
    home: Mapped[str] = mapped_column(String(80))
    away: Mapped[str] = mapped_column(String(80))
    commence: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    snap: Mapped[str] = mapped_column(Text)
