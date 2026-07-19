"""Provisioning admin router — wipes the ``provisioning`` schema.

``fault_injection`` is reference data (enabled/disabled faults per task-type)
and is deliberately NOT listed — scenario tweaks to fault config persist
across runs and are reset separately by the fault-injection admin tool.
"""

from __future__ import annotations

from bss_admin import ResetPlan, TableReset, admin_router

_OPERATIONAL = (TableReset("provisioning_task"),)

router = admin_router(
    service_name="provisioning-sim",
    plans=[ResetPlan(schema="provisioning", tables=_OPERATIONAL)],
)
