"""Mediation admin router — wipes the ``mediation`` schema."""

from __future__ import annotations

from bss_admin import ResetPlan, TableReset, admin_router

_OPERATIONAL = (TableReset("usage_event"),)

router = admin_router(
    service_name="mediation",
    plans=[ResetPlan(schema="mediation", tables=_OPERATIONAL)],
)
