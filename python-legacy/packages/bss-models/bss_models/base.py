"""SQLAlchemy 2.0 declarative base with naming conventions and common mixins."""

from datetime import datetime

from sqlalchemy import DateTime, MetaData, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# TIMESTAMP WITH TIME ZONE — use this instead of TIMESTAMPTZ which isn't exported by SA.
TZDateTime = DateTime(timezone=True)

# Naming conventions so Alembic generates predictable constraint names.
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=convention)


class TenantMixin:
    """Every table carries a tenant_id for future multi-tenancy."""

    tenant_id: Mapped[str] = mapped_column(
        Text, nullable=False, default="DEFAULT", server_default="DEFAULT"
    )


class TimestampMixin:
    """created_at / updated_at for mutable tables."""

    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
