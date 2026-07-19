"""CRM admin router — wipes operational CRM rows + resets inventory pools.

The CRM service owns two schemas: ``crm`` and ``inventory``. Agents and
SLA policies (reference) stay; everything customer-facing is truncated.
The MSISDN and eSIM pools are *not* truncated — they are seeded reference
data — but assignment columns are cleared so numbers are reusable.
"""

from __future__ import annotations

from bss_admin import ResetPlan, TableReset, admin_router

# Operational CRM tables. The TRUNCATE ... CASCADE means order is mostly
# cosmetic, but listing child-before-parent keeps intent obvious.
_CRM_OPERATIONAL = (
    TableReset("ticket_state_history"),
    TableReset("ticket"),
    TableReset("case_note"),
    TableReset("case"),
    TableReset("interaction"),
    TableReset("customer_identity"),
    TableReset("customer"),
    TableReset("contact_medium"),
    TableReset("individual"),
    TableReset("party"),
)

# crm.agent, crm.sla_policy are reference — NOT listed, NOT touched.

_INVENTORY_POOL_RESETS = (
    TableReset(
        "msisdn_pool",
        mode="update",
        update_sql=(
            'UPDATE "inventory"."msisdn_pool" SET '
            "status = 'available', "
            "reserved_at = NULL, "
            "assigned_to_subscription_id = NULL, "
            "quarantine_until = NULL"
        ),
    ),
    TableReset(
        "esim_profile",
        mode="update",
        update_sql=(
            'UPDATE "inventory"."esim_profile" SET '
            "profile_state = 'available', "
            "assigned_msisdn = NULL, "
            "assigned_to_subscription_id = NULL, "
            "reserved_at = NULL, "
            "downloaded_at = NULL, "
            "activated_at = NULL"
        ),
    ),
)

router = admin_router(
    service_name="crm",
    plans=[
        ResetPlan(schema="crm", tables=_CRM_OPERATIONAL),
        ResetPlan(schema="inventory", tables=_INVENTORY_POOL_RESETS),
    ],
)
