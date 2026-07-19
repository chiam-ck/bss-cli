"""COM admin router — wipes the ``order_mgmt`` schema."""

from __future__ import annotations

from bss_admin import ResetPlan, TableReset, admin_router

_OPERATIONAL = (
    TableReset("order_state_history"),
    TableReset("order_item"),
    TableReset("product_order"),
)

router = admin_router(
    service_name="com",
    plans=[ResetPlan(schema="order_mgmt", tables=_OPERATIONAL)],
)
