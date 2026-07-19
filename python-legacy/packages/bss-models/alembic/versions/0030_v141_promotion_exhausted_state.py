"""v1.4.1: add ``exhausted`` to catalog.promotion.state CHECK constraint.

``exhausted`` is an operator-initiated terminal state (``bss promo exhaust``
flips ``active → exhausted``) used to stop a promo from being applied to new
orders without retiring the row outright. ``validate_for_order`` and
``resolve_eligible_promo`` reject ``exhausted`` promos the same way they
reject non-``active`` rows today — order proceeds at full price.

Until v1.4.1 the CHECK constraint only allowed
``pending_link | active | retired``; ``exhausted`` writes would have failed
with a constraint violation. This migration replaces the constraint to
include the new value.

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-25
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "catalog"


def upgrade() -> None:
    op.drop_constraint("ck_promotion_state", "promotion", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_promotion_state",
        "promotion",
        "state IN ('pending_link','active','retired','exhausted')",
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Pre-flight: refuse downgrade if any rows are in ``exhausted`` — the
    # narrower constraint would reject them and the migration would die
    # mid-run, leaving a partially-applied schema.
    conn = op.get_bind()
    n = conn.exec_driver_sql(
        "SELECT count(*) FROM catalog.promotion WHERE state = 'exhausted'"
    ).scalar_one()
    if n:
        raise RuntimeError(
            f"refuse to downgrade: {n} promotion(s) in 'exhausted' state. "
            f"Move them to 'retired' first, then re-run downgrade."
        )
    op.drop_constraint("ck_promotion_state", "promotion", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_promotion_state",
        "promotion",
        "state IN ('pending_link','active','retired')",
        schema=SCHEMA,
    )
