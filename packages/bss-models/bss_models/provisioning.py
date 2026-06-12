"""Provisioning (simulator) schema — 2 tables.

provisioning_task, fault_injection.
"""

from datetime import datetime

from sqlalchemy import Boolean, Numeric, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, TimestampMixin, TZDateTime

SCHEMA = "provisioning"


class ProvisioningTask(Base, TenantMixin, TimestampMixin):
    __tablename__ = "provisioning_task"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    service_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3, server_default="3")
    payload: Mapped[dict | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class FaultInjection(Base, TenantMixin, TimestampMixin):
    __tablename__ = "fault_injection"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    fault_type: Mapped[str] = mapped_column(Text, nullable=False)
    probability: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
