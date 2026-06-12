"""Order Management (COM) schema — 3 tables.

product_order, order_item, order_state_history.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Numeric, SmallInteger, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TenantMixin, TimestampMixin, TZDateTime

SCHEMA = "order_mgmt"


class ProductOrder(Base, TenantMixin, TimestampMixin):
    __tablename__ = "product_order"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="acknowledged")
    order_date: Mapped[datetime | None] = mapped_column(TZDateTime)
    requested_completion_date: Mapped[datetime | None] = mapped_column(TZDateTime)
    completed_date: Mapped[datetime | None] = mapped_column(TZDateTime)
    msisdn_preference: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # v1.2 — set by the reconciliation sweeper when it emits order.stuck for an
    # order left in_progress past the threshold, so the signal fires at most once.
    stuck_flagged_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")
    state_history: Mapped[list["OrderStateHistory"]] = relationship(back_populates="order")


class OrderItem(Base, TenantMixin, TimestampMixin):
    __tablename__ = "order_item"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    order_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.product_order.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    offering_id: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str | None] = mapped_column(Text)
    target_subscription_id: Mapped[str | None] = mapped_column(Text)

    # v0.7 — price snapshot stamped at order creation, copied to subscription.
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    price_currency: Mapped[str | None] = mapped_column(Text)
    price_offering_price_id: Mapped[str | None] = mapped_column(Text)

    # v1.1 — promo discount captured at order create (INTENT, not yet claimed),
    # carried into the subscription snapshot at activation. discount_code is the
    # typed non-targeted code (NULL for targeted/no-promo). promo_offer_id is
    # the loyalty offer id captured at claim — used for redeem/revoke.
    discount_code: Mapped[str | None] = mapped_column(Text)
    promo_offer_definition_id: Mapped[str | None] = mapped_column(Text)
    discount_type: Mapped[str | None] = mapped_column(Text)  # percent | absolute
    discount_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    discount_periods_total: Mapped[int | None] = mapped_column(SmallInteger)
    promo_offer_id: Mapped[str | None] = mapped_column(Text)

    order: Mapped["ProductOrder"] = relationship(back_populates="items")


class OrderStateHistory(Base, TenantMixin):
    __tablename__ = "order_state_history"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.product_order.id"), nullable=False
    )
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    event_time: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    order: Mapped["ProductOrder"] = relationship(back_populates="state_history")
