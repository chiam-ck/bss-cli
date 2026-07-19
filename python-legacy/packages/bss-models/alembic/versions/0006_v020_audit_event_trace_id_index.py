"""v0.2.0: index audit.domain_event.trace_id for `bss trace for-*` lookups.

The trace_id column itself was created in 0001_initial. v0.2 adds the
index because the new `bss trace for-order ORD-014` resolution path
queries audit.domain_event by trace_id, and unindexed text scans on
the audit table get expensive fast.

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-22
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_domain_event_trace_id",
        "domain_event",
        ["trace_id"],
        schema="audit",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_domain_event_trace_id",
        table_name="domain_event",
        schema="audit",
    )
