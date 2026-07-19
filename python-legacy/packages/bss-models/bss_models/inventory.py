"""Inventory schema — 2 tables.

msisdn_pool, esim_profile.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, TimestampMixin, TZDateTime

SCHEMA = "inventory"


class MsisdnPool(Base, TenantMixin, TimestampMixin):
    __tablename__ = "msisdn_pool"
    __table_args__ = {"schema": SCHEMA}

    msisdn: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="available")
    reserved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    assigned_to_subscription_id: Mapped[str | None] = mapped_column(Text)
    quarantine_until: Mapped[datetime | None] = mapped_column(TZDateTime)


class EsimProfile(Base, TenantMixin, TimestampMixin):
    __tablename__ = "esim_profile"
    __table_args__ = {"schema": SCHEMA}

    iccid: Mapped[str] = mapped_column(Text, primary_key=True)
    imsi: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # NEVER store actual Ki values. ki_ref is a reference to a hypothetical HSM slot.
    # Real Ki storage is HSM territory and explicitly out of scope for BSS-CLI.
    ki_ref: Mapped[str] = mapped_column(Text, nullable=False)
    profile_state: Mapped[str] = mapped_column(Text, nullable=False, default="available")
    smdp_server: Mapped[str | None] = mapped_column(Text)
    matching_id: Mapped[str | None] = mapped_column(Text, unique=True)
    activation_code: Mapped[str | None] = mapped_column(Text)
    assigned_msisdn: Mapped[str | None] = mapped_column(
        Text, ForeignKey(f"{SCHEMA}.msisdn_pool.msisdn")
    )
    assigned_to_subscription_id: Mapped[str | None] = mapped_column(Text)
    reserved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    downloaded_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    activated_at: Mapped[datetime | None] = mapped_column(TZDateTime)
