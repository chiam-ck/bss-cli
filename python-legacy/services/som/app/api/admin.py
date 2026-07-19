"""SOM admin router — wipes the ``service_inventory`` schema."""

from __future__ import annotations

from bss_admin import ResetPlan, TableReset, admin_router

_OPERATIONAL = (
    TableReset("service_state_history"),
    TableReset("service"),
    TableReset("service_order_item"),
    TableReset("service_order"),
)

router = admin_router(
    service_name="som",
    plans=[ResetPlan(schema="service_inventory", tables=_OPERATIONAL)],
)
