"""Subscription schema — 4 tables.

subscription, bundle_balance, vas_purchase, subscription_state_history.
"""

from datetime import datetime

from decimal import Decimal

from sqlalchemy import BigInteger, Computed, ForeignKey, Numeric, SmallInteger, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TZDateTime, TenantMixin, TimestampMixin

SCHEMA = "subscription"


class Subscription(Base, TenantMixin, TimestampMixin):
    __tablename__ = "subscription"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    offering_id: Mapped[str] = mapped_column(Text, nullable=False)
    # v1.2 — the originating commercial order. Idempotency key for create():
    # a redelivered service_order.completed returns the existing subscription
    # instead of charging the card-on-file a second time. Unique (partial) index.
    commercial_order_id: Mapped[str | None] = mapped_column(Text)
    msisdn: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    iccid: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    cfs_service_id: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    state_reason: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    current_period_start: Mapped[datetime | None] = mapped_column(TZDateTime)
    current_period_end: Mapped[datetime | None] = mapped_column(TZDateTime)
    next_renewal_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    terminated_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    # v0.18 — written by the renewal worker BEFORE dispatching renew() so a
    # peer replica or a re-fired tick doesn't double-charge the same period
    # boundary. Compared via `last_renewal_attempted_at < next_renewal_at` —
    # when next_renewal_at advances after a successful renewal the row
    # naturally becomes "due" again next period without any cleanup. Reused
    # by the blocked-overdue sweep so a single column dedup both signals.
    last_renewal_attempted_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    # v0.18 — set when the upcoming-renewal reminder email was sent.
    # Same dedup pattern; compared via
    # `renewal_reminder_sent_at < next_renewal_at`. Separate column from
    # last_renewal_attempted_at because the two signals fire at different
    # times in the period (reminder ~24h before, renewal at the boundary).
    renewal_reminder_sent_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    # v0.7 — price snapshot copied at order time. Renewal charges this, not catalog.
    price_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    price_currency: Mapped[str] = mapped_column(Text, nullable=False)
    price_offering_price_id: Mapped[str] = mapped_column(
        Text, ForeignKey("catalog.product_offering_price.id"), nullable=False
    )

    # v0.7 — pending plan change / price migration. Applied on next renewal.
    pending_offering_id: Mapped[str | None] = mapped_column(Text)
    pending_offering_price_id: Mapped[str | None] = mapped_column(Text)
    pending_effective_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    # v1.1 — promo discount snapshot. price_amount above stays the FULL base;
    # the effective charged amount is computed at charge time as
    # apply(discount, price_amount). Mirrors the pending_* plan-change fields:
    # same snapshot-on-the-row pattern, not a new one. renew() decrements
    # discount_periods_remaining while > 0; a pending plan change clears all
    # of these (a plan change ends the promo — DECISIONS 2026-05-21).
    discount_type: Mapped[str | None] = mapped_column(Text)  # percent | absolute
    discount_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # > 0 = periods left at the discounted price; 0 = none (full price);
    # -1 sentinel = perpetual (never decrements).
    discount_periods_remaining: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    promo_code: Mapped[str | None] = mapped_column(Text)  # forensic
    promo_offer_definition_id: Mapped[str | None] = mapped_column(Text)  # forensic / join

    balances: Mapped[list["BundleBalance"]] = relationship(back_populates="subscription")
    vas_purchases: Mapped[list["VasPurchase"]] = relationship(back_populates="subscription")
    state_history: Mapped[list["SubscriptionStateHistory"]] = relationship(
        back_populates="subscription"
    )


class BundleBalance(Base, TenantMixin, TimestampMixin):
    __tablename__ = "bundle_balance"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    subscription_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.subscription.id"), nullable=False
    )
    allowance_type: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consumed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    remaining: Mapped[int] = mapped_column(
        BigInteger, Computed("total - consumed", persisted=True)
    )
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(TZDateTime)
    period_end: Mapped[datetime | None] = mapped_column(TZDateTime)

    subscription: Mapped["Subscription"] = relationship(back_populates="balances")


class VasPurchase(Base, TenantMixin, TimestampMixin):
    __tablename__ = "vas_purchase"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    subscription_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.subscription.id"), nullable=False
    )
    vas_offering_id: Mapped[str] = mapped_column(Text, nullable=False)
    payment_attempt_id: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    allowance_added: Mapped[int] = mapped_column(BigInteger, nullable=False)
    allowance_type: Mapped[str] = mapped_column(Text, nullable=False)

    subscription: Mapped["Subscription"] = relationship(back_populates="vas_purchases")


class SubscriptionStateHistory(Base, TenantMixin):
    __tablename__ = "subscription_state_history"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subscription_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.subscription.id"), nullable=False
    )
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    event_time: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )

    subscription: Mapped["Subscription"] = relationship(back_populates="state_history")
