"""Service Inventory (SOM output, TMF638) schema — 4 tables.

service_order, service_order_item, service, service_state_history.
"""

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TenantMixin, TimestampMixin, TZDateTime

SCHEMA = "service_inventory"


class ServiceOrder(Base, TenantMixin, TimestampMixin):
    __tablename__ = "service_order"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    commercial_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="acknowledged")
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    items: Mapped[list["ServiceOrderItem"]] = relationship(back_populates="service_order")


class ServiceOrderItem(Base, TenantMixin, TimestampMixin):
    __tablename__ = "service_order_item"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    service_order_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.service_order.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    service_spec_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_service_id: Mapped[str | None] = mapped_column(Text)

    service_order: Mapped["ServiceOrder"] = relationship(back_populates="items")


class Service(Base, TenantMixin, TimestampMixin):
    __tablename__ = "service"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    subscription_id: Mapped[str | None] = mapped_column(Text)
    spec_id: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    parent_service_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.service.id")
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, default="feasibility_checked")
    characteristics: Mapped[dict | None] = mapped_column(JSONB)
    activated_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    terminated_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    parent: Mapped["Service | None"] = relationship(
        remote_side="Service.id", back_populates="children"
    )
    children: Mapped[list["Service"]] = relationship(back_populates="parent")
    state_history: Mapped[list["ServiceStateHistory"]] = relationship(back_populates="service")


class ServiceStateHistory(Base, TenantMixin):
    __tablename__ = "service_state_history"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    service_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.service.id"), nullable=False
    )
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    event_time: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    service: Mapped["Service"] = relationship(back_populates="state_history")
