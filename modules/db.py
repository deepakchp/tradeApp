"""
modules/db.py — PostgreSQL Persistence Layer
=============================================
SQLAlchemy ORM models for durable trade state persistence.
Every position state change, order event, and fill is written to PostgreSQL.
On startup, the system rebuilds state.positions from the DB.
"""

from __future__ import annotations

import json
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    create_engine, Column, Integer, Float, String, Boolean,
    DateTime, Date, Text, ForeignKey, Enum as SAEnum, JSON,
    Index,
)
from sqlalchemy.orm import (
    DeclarativeBase, Session, sessionmaker, relationship,
)

import structlog

from config import DATABASE_URL

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────
# SQLAlchemy Base & Engine
# ─────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


_engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5)
SessionLocal = sessionmaker(bind=_engine)


def init_db():
    """Create all tables if they don't exist."""
    Base.metadata.create_all(_engine)
    log.info("db.tables_created")


# ─────────────────────────────────────────────────────────────────
# ORM Models
# ─────────────────────────────────────────────────────────────────

class PositionRecord(Base):
    __tablename__ = "positions"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    position_id   = Column(String(64), unique=True, nullable=False, index=True)
    symbol        = Column(String(32), nullable=False, index=True)
    strategy      = Column(String(32), nullable=False)
    state         = Column(String(16), nullable=False, default="pending")
    entry_time    = Column(DateTime, default=datetime.utcnow)
    entry_spot    = Column(Float, default=0.0)
    entry_iv      = Column(Float, default=0.0)
    max_profit    = Column(Float, default=0.0)
    beta          = Column(Float, default=1.0)
    challenged_side_recentered = Column(Boolean, default=False)
    last_adjustment_ts = Column(Float, default=0.0)
    closed_at     = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    legs = relationship("LegRecord", back_populates="position", cascade="all, delete-orphan")
    events = relationship("OrderEventRecord", back_populates="position", cascade="all, delete-orphan")


class LegRecord(Base):
    __tablename__ = "legs"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    position_id  = Column(String(64), ForeignKey("positions.position_id"), nullable=False, index=True)
    symbol       = Column(String(64), nullable=False)
    strike       = Column(Float, nullable=False)
    expiry       = Column(Date, nullable=False)
    option_type  = Column(String(4), nullable=False)   # "CE" or "PE"
    is_long      = Column(Boolean, nullable=False)
    lots         = Column(Integer, nullable=False)
    lot_size     = Column(Integer, default=1)
    exchange     = Column(String(8), default="NFO")
    entry_price  = Column(Float, default=0.0)

    position = relationship("PositionRecord", back_populates="legs")


class OrderEventRecord(Base):
    __tablename__ = "order_events"
    __table_args__ = (
        Index("ix_order_events_ts", "timestamp"),
    )

    id           = Column(Integer, primary_key=True, autoincrement=True)
    position_id  = Column(String(64), ForeignKey("positions.position_id"), nullable=False, index=True)
    event_type   = Column(String(48), nullable=False)
    action       = Column(String(48), nullable=True)
    leg_symbol   = Column(String(64), nullable=True)
    fill_price   = Column(Float, nullable=True)
    order_id     = Column(String(64), nullable=True)
    details_json = Column(Text, nullable=True)
    timestamp    = Column(DateTime, default=datetime.utcnow)

    position = relationship("PositionRecord", back_populates="events")


# ─────────────────────────────────────────────────────────────────
# Persistence Functions
# ─────────────────────────────────────────────────────────────────

def persist_position(pos) -> None:
    """Upsert a Position dataclass into PostgreSQL."""
    from engine import Position  # Avoid circular import
    
    session = SessionLocal()
    try:
        record = session.query(PositionRecord).filter_by(position_id=pos.position_id).first()
        if record:
            # Update existing
            record.state = pos.state.value if hasattr(pos.state, 'value') else pos.state
            record.entry_spot = pos.entry_spot
            record.entry_iv = pos.entry_iv
            record.max_profit = pos.max_profit
            record.beta = pos.beta
            record.challenged_side_recentered = pos.challenged_side_recentered
            record.last_adjustment_ts = pos.last_adjustment_ts
            record.updated_at = datetime.utcnow()
            if pos.state.value == "closed" and record.closed_at is None:
                record.closed_at = datetime.utcnow()
        else:
            # Insert new
            record = PositionRecord(
                position_id=pos.position_id,
                symbol=pos.symbol,
                strategy=pos.strategy.value if hasattr(pos.strategy, 'value') else pos.strategy,
                state=pos.state.value if hasattr(pos.state, 'value') else pos.state,
                entry_time=pos.entry_time,
                entry_spot=pos.entry_spot,
                entry_iv=pos.entry_iv,
                max_profit=pos.max_profit,
                beta=pos.beta,
                challenged_side_recentered=pos.challenged_side_recentered,
                last_adjustment_ts=pos.last_adjustment_ts,
            )
            session.add(record)
            session.flush()

            # Insert legs
            for leg in pos.legs:
                leg_rec = LegRecord(
                    position_id=pos.position_id,
                    symbol=leg.symbol,
                    strike=leg.strike,
                    expiry=leg.expiry,
                    option_type=leg.option_type.value if hasattr(leg.option_type, 'value') else leg.option_type,
                    is_long=leg.is_long,
                    lots=leg.lots,
                    lot_size=leg.lot_size,
                    exchange=leg.exchange,
                    entry_price=leg.entry_price,
                )
                session.add(leg_rec)

        session.commit()
        log.info("db.position_persisted", position_id=pos.position_id, state=pos.state.value)
    except Exception as e:
        session.rollback()
        log.error("db.persist_position_failed", position_id=pos.position_id, error=str(e))
    finally:
        session.close()


def persist_order_event(
    position_id: str,
    event_type: str,
    action: Optional[str] = None,
    leg_symbol: Optional[str] = None,
    fill_price: Optional[float] = None,
    order_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Append an order lifecycle event to the database."""
    session = SessionLocal()
    try:
        event = OrderEventRecord(
            position_id=position_id,
            event_type=event_type,
            action=action,
            leg_symbol=leg_symbol,
            fill_price=fill_price,
            order_id=order_id,
            details_json=json.dumps(details) if details else None,
        )
        session.add(event)
        session.commit()
    except Exception as e:
        session.rollback()
        log.error("db.persist_event_failed", position_id=position_id, event=event_type, error=str(e))
    finally:
        session.close()


def load_all_positions() -> Dict[str, Any]:
    """
    Load all non-closed positions from the database and rebuild
    engine.Position dataclass instances.
    Returns dict[position_id -> Position].
    """
    from engine import (
        Position, OptionLeg, PositionState, StrategyType, OptionType, Greeks,
    )

    session = SessionLocal()
    positions = {}
    try:
        records = (
            session.query(PositionRecord)
            .filter(PositionRecord.state.notin_(["closed"]))
            .all()
        )
        for rec in records:
            legs = []
            for leg_rec in rec.legs:
                opt_type = OptionType(leg_rec.option_type)
                leg = OptionLeg(
                    symbol=leg_rec.symbol,
                    strike=leg_rec.strike,
                    expiry=leg_rec.expiry,
                    option_type=opt_type,
                    is_long=leg_rec.is_long,
                    lots=leg_rec.lots,
                    lot_size=leg_rec.lot_size,
                    exchange=leg_rec.exchange,
                    entry_price=leg_rec.entry_price,
                    current_price=leg_rec.entry_price,  # Will be updated on first tick
                    greeks=Greeks(),
                )
                legs.append(leg)

            pos = Position(
                position_id=rec.position_id,
                symbol=rec.symbol,
                strategy=StrategyType(rec.strategy),
                legs=legs,
                state=PositionState(rec.state),
                entry_time=rec.entry_time or datetime.utcnow(),
                entry_spot=rec.entry_spot or 0.0,
                entry_iv=rec.entry_iv or 0.0,
                max_profit=rec.max_profit or 0.0,
                beta=rec.beta or 1.0,
                last_adjustment_ts=rec.last_adjustment_ts or 0.0,
                challenged_side_recentered=rec.challenged_side_recentered or False,
            )
            positions[pos.position_id] = pos

        log.info("db.positions_loaded", count=len(positions))
    except Exception as e:
        log.error("db.load_positions_failed", error=str(e))
    finally:
        session.close()

    return positions
