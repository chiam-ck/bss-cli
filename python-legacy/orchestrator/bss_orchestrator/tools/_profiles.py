"""Tool profiles — per-surface curated subsets of TOOL_REGISTRY (v0.12).

Default behaviour for ``astream_once`` (no profile specified) remains
full-tool access — CLI, scenarios, and the v0.5 CSR console keep
their current capability. Only the chat surface invokes with
``tool_filter="customer_self_serve"`` so the LLM can act only on the
logged-in customer.

The registry is curated, not generated. Adding a tool is a security
review: each entry widens the chat's autonomous reach, so each entry
must be ownership-bound (either via a ``*.mine`` wrapper, or because
the tool is a public catalog read with no customer-bound output).

Greppable: every name in this module's value sets must resolve in
``TOOL_REGISTRY`` at startup. ``validate_profiles()`` enforces this
plus the "no customer_id parameter" rule for ``*.mine`` wrappers.

v0.20+: knowledge.* entries are gated on BSS_KNOWLEDGE_ENABLED. When
the env var is false, the entries are absent from TOOL_PROFILES so
validate_profiles() doesn't trip on a missing-from-registry tool.
The env read is at import time (consistent with token / provider
patterns); flip the env + restart to toggle.
"""

from __future__ import annotations

import os as _os

_KNOWLEDGE_ENABLED = (
    _os.environ.get("BSS_KNOWLEDGE_ENABLED", "true").strip().lower()
    in {"1", "true", "yes", "on"}
)

import inspect

from ._registry import TOOL_REGISTRY

# Parameter names a *.mine wrapper must NOT accept. The wrapper binds
# customer_id from auth_context.current().actor; accepting it as a
# parameter would let a prompt-injected LLM target another customer
# even though every server-side policy still blocks the cross-customer
# attempt. The seam exists so that prompt-injection containment does
# not depend on the LLM behaving.
FORBIDDEN_MINE_PARAMETERS: frozenset[str] = frozenset(
    {
        "customer_id",
        "customer_email",
        "msisdn",
    }
)


TOOL_PROFILES: dict[str, set[str]] = {
    # v0.12 — the chat surface profile. Each tool is either a
    # public catalog read or a ``*.mine``/``*_for_me`` wrapper that
    # binds customer_id from auth_context.
    #
    # PR2 shipped the read-only tools.
    # PR3 added the four write *.mine wrappers.
    # PR6 will add case.open_for_me.
    "customer_self_serve": {
        # public catalog reads — no customer-bound output, safe in any
        # session
        "catalog.list_vas",
        "catalog.list_active_offerings",
        "catalog.get_offering",
        # read .mine wrappers (PR2)
        "subscription.list_mine",
        "subscription.get_mine",
        "subscription.get_balance_mine",
        "subscription.get_lpa_mine",
        "usage.history_mine",
        "customer.get_mine",
        "payment.method_list_mine",
        "payment.charge_history_mine",
        # write .mine wrappers (PR3)
        "vas.purchase_for_me",
        "subscription.schedule_plan_change_mine",
        "subscription.cancel_pending_plan_change_mine",
        "subscription.terminate_mine",
        # case open (PR6) — escalation to a human via the five
        # non-negotiable categories.
        "case.open_for_me",
        # case list (v0.13.1) — customer asks "what's my case ID?" or
        # "is my case still open?". Without this the LLM hallucinates
        # past-tense escalation language because it has no tool to
        # answer with real data.
        "case.list_for_me",
    },

    # v0.13 — the operator cockpit profile. Full registry coverage
    # MINUS the customer-side ``*.mine`` / ``*_for_me`` wrappers (the
    # operator binds via ``actor=settings.actor``, not via customer_id
    # scoping). Doctrine: this is a coverage assertion, not a
    # restriction set. If a new tool ships and isn't listed here, the
    # cockpit can't see it — forces conscious inclusion. Startup
    # ``validate_profiles()`` resolves every entry against
    # TOOL_REGISTRY; deploy fails fast on drift.
    #
    # Three things expressly excluded:
    # 1. ``*.mine`` / ``*_for_me`` wrappers (customer-side scoping
    #    seam, irrelevant to the operator).
    # 2. There are none of the v0.12 enforcement helpers (this is
    #    not a restriction profile).
    # 3. Nothing partial — adding a tool to TOOL_REGISTRY without
    #    adding it here is a drift the doctrine guard catches at
    #    next test run.
    "operator_cockpit": {
        # ── reads ────────────────────────────────────────────────
        # CRM
        "customer.get",
        "customer.list",
        "customer.find_by_msisdn",
        "customer.find_by_email",
        "customer.get_kyc_status",
        "case.get",
        "case.list",
        "case.show_transcript_for",
        "ticket.get",
        "ticket.list",
        "interaction.list",
        # Catalog
        "catalog.list_active_offerings",
        "catalog.list_offerings",
        "catalog.get_offering",
        "catalog.get_active_price",
        "catalog.list_vas",
        "catalog.get_vas",
        # Subscription / service
        "subscription.list_for_customer",
        "subscription.get",
        "subscription.get_balance",
        "subscription.get_esim_activation",
        "service.get",
        "service.list_for_subscription",
        # Orders / SOM
        "order.get",
        "order.list",
        "order.wait_until",
        "service_order.get",
        "service_order.list_for_order",
        # Payment
        "payment.list_methods",
        "payment.list_attempts",
        "payment.get_attempt",
        # Inventory
        "inventory.msisdn.list_available",
        "inventory.msisdn.count",
        "inventory.msisdn.get",
        "inventory.esim.list_available",
        "inventory.esim.get_activation",
        # Provisioning sim
        "provisioning.get_task",
        "provisioning.list_tasks",
        # Usage / mediation
        "usage.history",
        # Trace + ops
        "trace.get",
        "trace.for_order",
        "trace.for_subscription",
        "events.list",
        "agents.list",
        "clock.now",

        # ── writes ───────────────────────────────────────────────
        # CRM
        "customer.create",
        "customer.update_contact",
        "customer.add_contact_medium",
        "customer.remove_contact_medium",
        "customer.attest_kyc",
        "customer.close",
        "interaction.log",
        # Cases + tickets
        "case.open",
        "case.close",
        "case.add_note",
        "case.transition",
        "case.update_priority",
        "ticket.open",
        "ticket.assign",
        "ticket.transition",
        "ticket.resolve",
        "ticket.close",
        "ticket.cancel",
        # Catalog admin
        "catalog.add_offering",
        "catalog.add_price",
        "catalog.window_offering",
        # Promotions (v1.1) — operator-only by doctrine; never customer_self_serve.
        "promo.create",
        "promo.assign",
        "promo.show",
        # Subscription writes
        "subscription.terminate",
        "subscription.schedule_plan_change",
        "subscription.cancel_pending_plan_change",
        "subscription.migrate_to_new_price",
        "subscription.purchase_vas",
        "subscription.renew_now",
        "subscription.tick_renewals_now",
        # Orders
        "order.create",
        "order.cancel",
        # Payment
        "payment.add_card",
        "payment.remove_method",
        "payment.charge",
        # Inventory ops (v0.17)
        "inventory.msisdn.add_range",
        # Port-request ops (v0.17 MNP — operator-only by spec)
        "port_request.list",
        "port_request.get",
        "port_request.create",
        "port_request.approve",
        "port_request.reject",
        # Provisioning ops
        "provisioning.resolve_stuck",
        "provisioning.retry_failed",
        "provisioning.set_fault_injection",
        # Test/scenario plumbing — operator may freeze/advance for
        # local repro and demo timelines. Not OCS-grade; doctrine
        # accepts.
        "clock.advance",
        "clock.freeze",
        "clock.unfreeze",
        "usage.simulate",
    },
}

# v0.20+ — knowledge tools (operator_cockpit only). Customer chat does
# NOT get these by doctrine: handbook + runbooks describe destructive
# operator flows + perimeter posture, neither belongs in customer chat.
# validate_profiles() enforces customer_self_serve excludes them.
# Gated on BSS_KNOWLEDGE_ENABLED so disabled deployments don't trip
# the "tool in profile but not in TOOL_REGISTRY" assertion.
if _KNOWLEDGE_ENABLED:
    TOOL_PROFILES["operator_cockpit"].update(
        {
            "knowledge.search",
            "knowledge.get",
        }
    )


def get_profile(name: str) -> set[str]:
    """Return the tool name set for ``name``. KeyError if unknown."""
    return TOOL_PROFILES[name]


def is_mine_tool(tool_name: str) -> bool:
    """True for the ``*.mine`` and ``*_for_me`` wrapper names."""
    return tool_name.endswith(".mine") or tool_name.endswith("_mine") or tool_name.endswith("_for_me")


def validate_profiles() -> None:
    """Check every profile + wrapper invariant. Raise on violation.

    Called at orchestrator import time so deploys fail fast rather
    than at the first chat turn:

    1. Every name listed in any profile is registered in TOOL_REGISTRY.
    2. Every ``*.mine`` / ``*_for_me`` tool's signature does not accept
       a forbidden owner-bound parameter.
    3. Every tool in the ``customer_self_serve`` profile has an entry
       in ``OWNERSHIP_PATHS`` (use ``[]`` for tools whose response
       carries no customer-bound fields by contract).

    Raises:
        RuntimeError: profile drift (missing tool), signature
            violation (forbidden parameter), or OWNERSHIP_PATHS
            coverage gap.
    """
    for profile_name, tool_names in TOOL_PROFILES.items():
        for name in sorted(tool_names):
            if name not in TOOL_REGISTRY:
                raise RuntimeError(
                    f"Profile {profile_name!r} lists unregistered tool "
                    f"{name!r}. Either add the tool or remove from "
                    f"_profiles.py."
                )

    # *.mine signature inspection — applies across every registered
    # mine tool, not just those in the customer profile. A mine tool
    # that grew a customer_id parameter "for refactoring convenience"
    # is the worst regression we want to catch at deploy time.
    for name, fn in TOOL_REGISTRY.items():
        if not is_mine_tool(name):
            continue
        sig = inspect.signature(fn)
        forbidden = sorted(
            p for p in sig.parameters if p in FORBIDDEN_MINE_PARAMETERS
        )
        if forbidden:
            raise RuntimeError(
                f"Tool {name!r} accepts forbidden parameter(s) "
                f"{forbidden!r}. *.mine wrappers must bind customer_id "
                f"from auth_context.current().actor — never from a "
                f"caller-supplied parameter."
            )

    # v0.13 — operator_cockpit profile must NOT contain any mine
    # wrapper. The wrappers exist for prompt-injection containment on
    # the customer chat surface; the operator binds via
    # ``actor=settings.actor`` and has no ownership scoping. Mixing
    # the two would muddle the audit trail.
    cockpit = TOOL_PROFILES.get("operator_cockpit", set())
    bad_mine = sorted(t for t in cockpit if is_mine_tool(t))
    if bad_mine:
        raise RuntimeError(
            f"Profile 'operator_cockpit' lists *.mine / *_for_me "
            f"wrappers {bad_mine!r}. Those exist for the customer "
            f"chat surface; the operator cockpit binds via "
            f"actor=settings.actor and must never use them."
        )

    # v0.12 PR4 — every customer_self_serve tool has an OWNERSHIP_PATHS
    # entry. Imported lazily so this module stays decoupled from
    # ownership.py (which imports nothing from here).
    from ..ownership import validate_ownership_paths_cover_profile

    validate_ownership_paths_cover_profile(TOOL_PROFILES["customer_self_serve"])
