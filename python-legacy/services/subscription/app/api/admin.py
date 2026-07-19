"""Subscription admin router — wipes the ``subscription`` schema."""

from __future__ import annotations

from bss_admin import ResetPlan, TableReset, admin_router

_OPERATIONAL = (
    TableReset("subscription_state_history"),
    TableReset("vas_purchase"),
    TableReset("bundle_balance"),
    TableReset("subscription"),
)

router = admin_router(
    service_name="subscription",
    plans=[ResetPlan(schema="subscription", tables=_OPERATIONAL)],
)
