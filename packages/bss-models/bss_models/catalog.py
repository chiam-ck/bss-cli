"""Catalog schema — 7 tables.

product_specification, product_offering, product_offering_price,
bundle_allowance, vas_offering, service_specification, product_to_service_mapping.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Numeric,
    SmallInteger,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TZDateTime, TenantMixin, TimestampMixin

SCHEMA = "catalog"


class ProductSpecification(Base, TenantMixin, TimestampMixin):
    __tablename__ = "product_specification"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(Text)
    lifecycle_status: Mapped[str | None] = mapped_column(Text)

    offerings: Mapped[list["ProductOffering"]] = relationship(back_populates="specification")


class ProductOffering(Base, TenantMixin, TimestampMixin):
    __tablename__ = "product_offering"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    spec_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.product_specification.id")
    )
    is_bundle: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    is_sellable: Mapped[bool | None] = mapped_column(Boolean)
    lifecycle_status: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[datetime | None] = mapped_column(TZDateTime)
    valid_to: Mapped[datetime | None] = mapped_column(TZDateTime)

    specification: Mapped["ProductSpecification | None"] = relationship(back_populates="offerings")
    prices: Mapped[list["ProductOfferingPrice"]] = relationship(back_populates="offering")
    allowances: Mapped[list["BundleAllowance"]] = relationship(back_populates="offering")
    service_mappings: Mapped[list["ProductToServiceMapping"]] = relationship(back_populates="offering")


class ProductOfferingPrice(Base, TenantMixin, TimestampMixin):
    __tablename__ = "product_offering_price"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    offering_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.product_offering.id"), nullable=False
    )
    price_type: Mapped[str] = mapped_column(Text, nullable=False)
    recurring_period_length: Mapped[int | None] = mapped_column(SmallInteger)
    recurring_period_type: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="SGD")
    valid_from: Mapped[datetime | None] = mapped_column(TZDateTime)
    valid_to: Mapped[datetime | None] = mapped_column(TZDateTime)

    offering: Mapped["ProductOffering"] = relationship(back_populates="prices")


class BundleAllowance(Base, TenantMixin, TimestampMixin):
    __tablename__ = "bundle_allowance"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    offering_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.product_offering.id"), nullable=False
    )
    allowance_type: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)

    offering: Mapped["ProductOffering"] = relationship(back_populates="allowances")


class VasOffering(Base, TenantMixin, TimestampMixin):
    __tablename__ = "vas_offering"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    price_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="SGD")
    allowance_type: Mapped[str | None] = mapped_column(Text)
    allowance_quantity: Mapped[int | None] = mapped_column(BigInteger)
    allowance_unit: Mapped[str | None] = mapped_column(Text)
    expiry_hours: Mapped[int | None] = mapped_column(SmallInteger)


class ServiceSpecification(Base, TenantMixin, TimestampMixin):
    __tablename__ = "service_specification"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(Text)
    parameters: Mapped[dict | None] = mapped_column(JSONB)


class Promotion(Base, TenantMixin, TimestampMixin):
    """v1.1 — money terms for a promotion + the join key to loyalty-cli.

    The only genuinely new BSS domain object in v1.1. loyalty owns the
    entitlement (codes/offers, validity windows, usage/per-customer limits,
    inventory, targeting); this row owns the *discount terms* and the join
    key (``offer_definition_id``). One promotion row per loyalty
    OfferDefinition; many codes/offers share it.

    No FK to loyalty — it lives behind the HTTP boundary. ``offer_definition_id``
    is NULL while the create saga is mid-flight (state ``pending_link``) and
    set once loyalty's ``offer_definition.register`` returns (state ``active``).
    A live code/offer does nothing until the row is ``active``, so a
    half-failed saga is harmless; ``promo reconcile`` relinks by OD.
    """

    __tablename__ = "promotion"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # e.g. PROMO_SUMMER25
    # v1.1.1 — operator-set friendly label for customer display (e.g. "VIP
    # Welcome"). NULL → UI falls back to the discount label. Same value sent to
    # loyalty's OfferDefinition display_name at create.
    name: Mapped[str | None] = mapped_column(Text)
    # The loyalty promo code. Present for BOTH audiences (v1.1.1): a public code
    # is advertised/typed; a targeted code is BSS-internal, auto-applied only for
    # eligible customers (gated by promotion_eligibility) and not advertised.
    code: Mapped[str | None] = mapped_column(Text)
    # public = anyone may type the code; targeted = eligibility-gated + auto-applied.
    audience: Mapped[str] = mapped_column(
        Text, nullable=False, default="public", server_default="public"
    )
    # The loyalty join key. NULL until the create saga completes.
    offer_definition_id: Mapped[str | None] = mapped_column(Text)
    discount_type: Mapped[str] = mapped_column(Text, nullable=False)  # percent | absolute
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, default="SGD", server_default="SGD"
    )  # for absolute discounts
    # NULL = applies to all sellable offerings.
    applicable_offering_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    duration_kind: Mapped[str] = mapped_column(Text, nullable=False)  # single | multi | perpetual
    # N for multi; NULL for single/perpetual.
    periods_total: Mapped[int | None] = mapped_column(SmallInteger)
    valid_from: Mapped[datetime | None] = mapped_column(TZDateTime)
    valid_to: Mapped[datetime | None] = mapped_column(TZDateTime)
    state: Mapped[str] = mapped_column(Text, nullable=False)  # pending_link → active → retired
    created_by: Mapped[str] = mapped_column(Text, nullable=False)


class PromotionEligibility(Base, TenantMixin, TimestampMixin):
    """v1.1.1 — which customers may use a *targeted* promotion's code.

    One code lives in loyalty; the per-customer pairing lives here (loyalty's
    promo_code has no customer field). BSS is the eligibility gate: a targeted
    code auto-applies for customers with a row here, and a typed targeted code
    is rejected for anyone without one. Public promos have no rows.
    """

    __tablename__ = "promotion_eligibility"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    promotion_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.promotion.id"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)


class ProductToServiceMapping(Base, TenantMixin, TimestampMixin):
    __tablename__ = "product_to_service_mapping"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    offering_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.product_offering.id"), nullable=False
    )
    cfs_spec_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.service_specification.id"), nullable=False
    )
    rfs_spec_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)

    offering: Mapped["ProductOffering"] = relationship(back_populates="service_mappings")
