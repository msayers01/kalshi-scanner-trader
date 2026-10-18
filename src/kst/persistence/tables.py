"""SQLAlchemy 2.0 ORM tables (one SQLite file per mode — see ARCHITECTURE.md §11)."""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import JSON, BigInteger, Boolean, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON}


class OrderRow(Base):
    __tablename__ = "orders"
    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(String(96), index=True)
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[int] = mapped_column(Integer)  # Price units ($0.0001)
    qty: Mapped[int] = mapped_column(BigInteger)  # Qty units (0.01 contract)
    filled: Mapped[int] = mapped_column(BigInteger, default=0)
    fees: Mapped[int] = mapped_column(BigInteger, default=0)  # micros
    status: Mapped[str] = mapped_column(String(16), index=True)
    tif: Mapped[str] = mapped_column(String(24))
    post_only: Mapped[bool] = mapped_column(Boolean, default=False)
    strategy: Mapped[str] = mapped_column(String(32), index=True)
    slot: Mapped[str] = mapped_column(String(32), default="")
    basket_id: Mapped[str] = mapped_column(String(32), default="")
    is_entry: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    created_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    updated_ms: Mapped[int] = mapped_column(BigInteger)


class FillRow(Base):
    __tablename__ = "fills"
    fill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(String(96), index=True)
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[int] = mapped_column(Integer)
    qty: Mapped[int] = mapped_column(BigInteger)
    fee: Mapped[int] = mapped_column(BigInteger)
    is_taker: Mapped[bool] = mapped_column(Boolean)
    strategy: Mapped[str] = mapped_column(String(32), index=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)


class SettlementRow(Base):
    __tablename__ = "settlements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(96), index=True)
    result: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(BigInteger)
    pnl: Mapped[int] = mapped_column(BigInteger)  # micros
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"
    ts_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cash: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    unrealized: Mapped[float] = mapped_column(Float)
    exposure: Mapped[float] = mapped_column(Float)
    pnl_today: Mapped[float] = mapped_column(Float)
    n_positions: Mapped[int] = mapped_column(Integer)


class DailyPnLRow(Base):
    __tablename__ = "daily_pnl"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    net_usd: Mapped[float] = mapped_column(Float)
    realized_usd: Mapped[float] = mapped_column(Float)
    fees_usd: Mapped[float] = mapped_column(Float)
    unrealized_usd: Mapped[float] = mapped_column(Float)
    end_equity_usd: Mapped[float] = mapped_column(Float)
    trades: Mapped[int] = mapped_column(Integer)
    round_trips: Mapped[int] = mapped_column(Integer)
    wins: Mapped[int] = mapped_column(Integer)
    losses: Mapped[int] = mapped_column(Integer)
    markets_entered: Mapped[int] = mapped_column(Integer)
    report: Mapped[dict[str, Any]] = mapped_column(JSON)


class LLMDecisionRow(Base):
    __tablename__ = "llm_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    ticker: Mapped[str] = mapped_column(String(96), index=True)
    verdict: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(2))
    reason: Mapped[str] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer)
    tokens_out: Mapped[int] = mapped_column(Integer)
    packet_hash: Mapped[str] = mapped_column(String(16))


class AuditRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class KVRow(Base):
    __tablename__ = "kv_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_ms: Mapped[int] = mapped_column(BigInteger)


Index("ix_fills_ticker_ts", FillRow.ticker, FillRow.ts_ms)
