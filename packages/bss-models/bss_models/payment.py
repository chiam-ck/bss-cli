"""Payment schema — 3 tables.

payment_method, payment_attempt, customer (v0.16+).

v0.16 introduces ``payment.customer`` — a thin per-(BSS customer,
provider) cache of the provider-side customer reference (Stripe
``cus_*``). It lets ``StripeTokenizerAdapter.ensure_customer`` skip
a Stripe round-trip on every charge after the first. The CRM customer
remains the authoritative customer record; this row is just a
provider-id cache.
"""

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Numeric, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TenantMixin, TimestampMixin, TZDateTime

SCHEMA = "payment"


class PaymentMethod(Base, TenantMixin, TimestampMixin):
    __tablename__ = "payment_method"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False, default="card")
    token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # v0.16+ : 'mock' (tok_<uuid>) | 'stripe' (pm_*). Lazy-fail cutover
    # behavior keys on this column — a 'mock' row attempted under
    # BSS_PAYMENT_PROVIDER=stripe raises mock_token_in_stripe_mode
    # cleanly; Track 4 (cutover CLI) operates on it.
    token_provider: Mapped[str] = mapped_column(
        Text, nullable=False, default="mock", server_default="mock"
    )
    last4: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str | None] = mapped_column(Text)
    exp_month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    exp_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")

    attempts: Mapped[list["PaymentAttempt"]] = relationship(back_populates="payment_method")


class PaymentAttempt(Base, TenantMixin, TimestampMixin):
    __tablename__ = "payment_attempt"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    payment_method_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.payment_method.id"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="SGD")
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    gateway_ref: Mapped[str | None] = mapped_column(Text)
    decline_reason: Mapped[str | None] = mapped_column(Text)
    # v0.16+ : provider-side primary key for the call (Stripe pi_*).
    # Joins to integrations.external_call.provider_call_id for forensic
    # `bss external-calls` queries.
    provider_call_id: Mapped[str | None] = mapped_column(Text)
    # v0.16+ : Stripe's machine-readable decline taxonomy
    # (insufficient_funds, card_declined, expired_card, ...). Stable
    # identifier for downstream conditionals; human reason stays in
    # `decline_reason`.
    decline_code: Mapped[str | None] = mapped_column(Text)
    # v0.16+ : ATT-{id}-r{retry_count}. Persisted for forensic
    # `bss external-calls --idempotency-key X` lookup AND as the
    # foundation for the v1.0 crash-recovery path (re-read on restart;
    # same key sent to provider → provider dedupes). Nullable for
    # back-compat with pre-v0.16 rows that have no key.
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    attempted_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    payment_method: Mapped["PaymentMethod"] = relationship(back_populates="attempts")


class PaymentCustomer(Base, TenantMixin, TimestampMixin):
    """Per-(BSS customer, provider) cache of the provider-side customer ref.

    ``id`` is the BSS customer id (CUST-001). ``customer_external_ref``
    is the provider-side primary key (Stripe ``cus_*``). The compound
    unique constraint on ``(id, customer_external_ref_provider)`` lets
    a future multi-provider deployment cache ``cus_*`` for Stripe and
    a different ``ext_ref`` for Adyen on the same BSS customer without
    collision.
    """

    __tablename__ = "customer"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_external_ref: Mapped[str | None] = mapped_column(Text)
    # 'stripe' (v0.16) | future: 'adyen', 'checkout', ...
    customer_external_ref_provider: Mapped[str | None] = mapped_column(Text)
