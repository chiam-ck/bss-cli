"""Billing schema — 2 tables.

billing_account, customer_bill.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TenantMixin, TimestampMixin, TZDateTime

SCHEMA = "billing"


class BillingAccount(Base, TenantMixin, TimestampMixin):
    __tablename__ = "billing_account"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    payment_method_id: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="SGD")

    bills: Mapped[list["CustomerBill"]] = relationship(back_populates="billing_account")


class CustomerBill(Base, TenantMixin, TimestampMixin):
    __tablename__ = "customer_bill"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    billing_account_id: Mapped[str] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.billing_account.id"), nullable=False
    )
    subscription_id: Mapped[str | None] = mapped_column(Text)
    period_start: Mapped[datetime | None] = mapped_column(TZDateTime)
    period_end: Mapped[datetime | None] = mapped_column(TZDateTime)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="SGD")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="issued")
    payment_attempt_id: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    paid_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    billing_account: Mapped["BillingAccount"] = relationship(back_populates="bills")
