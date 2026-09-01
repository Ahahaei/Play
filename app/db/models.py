from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.engine import Base


class SellerRow(Base):
    __tablename__ = "sellers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    slack_channel_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    slack_user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, unique=True, index=True)
    policies: Mapped[Any] = mapped_column(JSON, nullable=False)
    slack_credentials: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)


class SellerPlatformAccountRow(Base):
    """One seller's account on one platform, keyed by the platform-native id."""

    __tablename__ = "seller_platform_accounts"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_account_platform_external_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    seller_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    credentials: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventRow(Base):
    __tablename__ = "events"
    # NULLs do not collide, so internally-originated events (chat, L2 signals)
    # carry no platform/dedup_key and are never deduplicated against each other.
    __table_args__ = (
        UniqueConstraint("platform", "dedup_key", name="uq_event_platform_dedup_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    seller_id: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    platform: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    dedup_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    raw_payload: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobRow(Base):
    """Durable work queue. Claimed by the worker process, never by the API."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_run_after", "status", "run_after"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    seller_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # The absolute quantity computed before the write. A retry rewrites this
    # value instead of re-reading stock and compounding the delta (FIX.md A8).
    target_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, nullable=False)
    seller_id: Mapped[str] = mapped_column(String, nullable=False)
    intent: Mapped[str] = mapped_column(String, nullable=False)
    policy_result: Mapped[Any] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    slack_channel_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    slack_ts: Mapped[Optional[str]] = mapped_column(String, nullable=True)
