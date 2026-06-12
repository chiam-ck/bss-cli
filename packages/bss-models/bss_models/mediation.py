"""Mediation schema — 1 table.

usage_event.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, TimestampMixin, TZDateTime

SCHEMA = "mediation"


class UsageEvent(Base, TenantMixin, TimestampMixin):
    __tablename__ = "usage_event"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    msisdn: Mapped[str] = mapped_column(Text, nullable=False)
    subscription_id: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_time: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    raw_cdr_ref: Mapped[str | None] = mapped_column(Text)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    processing_error: Mapped[str | None] = mapped_column(Text)
    # v0.17 — set by the channel/network adapter when the underlying CDR
    # was produced on a visited (roaming) network. Mediation passes it
    # through to rating; rating routes the decrement to the
    # `data_roaming` BundleBalance instead of `data`. Default false so
    # pre-v0.17 callers (and every existing scenario) keep working.
    roaming_indicator: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
