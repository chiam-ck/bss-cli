"""Semantic vocabulary for the BSS-CLI LLM tool surface.

Every tool signature MUST use these aliases — never raw ``str`` for IDs or enums.

Why it matters: the LLM sees the Annotated metadata in the JSON schema emitted
for each tool. When the type says ``Annotated[str, "Subscription ID with the
SUB- prefix"]``, a small cheap model generates valid SUB- IDs instead of
fabricating ``sub-abc-123``. The type hint IS the semantic hint.

v0.13.1 — descriptions emphasize "opaque suffix; pass through verbatim" so
the LLM doesn't refuse hex-style ids (e.g. CASE-3360cc22) it expected to
match a "CASE-NNN" pattern. The actual id format varies by aggregate
(some sequential 4-digit, some 8-hex); the LLM should never validate or
"correct" the suffix.

For enum-shaped fields, ``Literal[...]`` renders as a JSON-schema enum and the
LLM respects the finite set.

Grep-enforced: ``test_types_coverage.py`` walks every tool and confirms no raw
``str`` is used for an ID or enum argument where an alias exists here.
"""

from __future__ import annotations

from typing import Annotated, Literal

# ─────────────────────────────────────────────────────────────────────────────
# ID types — Annotated[str, "format hint"]
# ─────────────────────────────────────────────────────────────────────────────

CustomerId = Annotated[
    str,
    "Customer ID with the CUST- prefix (e.g. CUST-fbcd3c87). The "
    "suffix is opaque — never validate or rewrite it. Pass through "
    "verbatim from a prior tool result (customer.list, "
    "customer.find_by_msisdn, customer.find_by_email, "
    "customer.create). Never fabricate.",
]
SubscriptionId = Annotated[
    str,
    "Subscription ID with the SUB- prefix (e.g. SUB-0937). The "
    "suffix is opaque. Pass through verbatim from "
    "subscription.list_for_customer or similar reads.",
]
OrderId = Annotated[
    str,
    "Product Order ID with the ORD- prefix (e.g. ORD-0349). The "
    "suffix is opaque. Pass through verbatim from order.list or "
    "order.create.",
]
ServiceOrderId = Annotated[
    str,
    "Service Order ID with the SO- prefix. The suffix is opaque. "
    "Pass through verbatim from service_order.list_for_order.",
]
ServiceId = Annotated[
    str,
    "Service ID with the SVC- prefix (e.g. SVC-0755). The suffix is "
    "opaque. Pass through verbatim from service.list_for_subscription.",
]
CaseId = Annotated[
    str,
    "Case ID with the CASE- prefix (e.g. CASE-3360cc22). The suffix "
    "is opaque — never validate, rewrite, or 'correct' it. Pass "
    "through verbatim from case.list / case.list_for_me / case.open / "
    "case.open_for_me.",
]
TicketId = Annotated[
    str,
    "Ticket ID with the TKT- prefix. The suffix is opaque. Pass "
    "through verbatim from ticket.list.",
]
ContactMediumId = Annotated[
    str,
    "Contact Medium ID with the CM- prefix. The suffix is opaque. "
    "Pass through verbatim from customer.get.",
]
PaymentMethodId = Annotated[
    str,
    "Payment Method ID with the PM- prefix. The suffix is opaque. "
    "Pass through verbatim from payment.list_methods.",
]
PaymentAttemptId = Annotated[
    str,
    "Payment Attempt ID with the PAY- prefix. The suffix is opaque.",
]
AgentId = Annotated[
    str,
    "Agent ID with the AGT- prefix. The suffix is opaque. Pass "
    "through verbatim from agents.list.",
]
ProvisioningTaskId = Annotated[
    str,
    "Provisioning task ID with the PTK- prefix. The suffix is opaque. "
    "Pass through verbatim from provisioning.list_tasks.",
]
PortRequestId = Annotated[
    str,
    "Port request ID with the PORT- prefix (e.g. PORT-3360CC22). The "
    "suffix is opaque. Pass through verbatim from port_request.list / "
    "port_request.create. Never fabricate.",
]
AggregateId = Annotated[
    str,
    "Prefixed ID matching the aggregate_type (SUB- / ORD- / CASE- / "
    "etc.). The suffix is opaque. Never fabricate — read from a "
    "prior tool result.",
]
ProductOfferingId = Annotated[
    str,
    "Plan offering ID, e.g. PLAN_S, PLAN_M, PLAN_L (v1.0 baseline). "
    "Get the full active set from catalog.list_active_offerings — "
    "operators can add new plans (PLAN_XL, PLAN_CNY, etc.) at runtime; "
    "never hardcode. Never fabricate.",
]
ProductOfferingPriceId = Annotated[
    str,
    "Product offering price ID, e.g. PRICE_PLAN_M or PRICE_PLAN_M_V2. "
    "Get from catalog.list_offerings (each offering carries its prices) — "
    "never fabricate. Used by snapshot-bound flows like price migration.",
]
VasOfferingId = Annotated[
    str,
    "VAS offering ID, e.g. VAS_DATA_5GB, VAS_DATA_DAYPASS. "
    "Get from catalog.list_vas — never guess.",
]
PromotionId = Annotated[
    str,
    "Promotion ID, e.g. PROMO_SUMMER25. Stable, operator-chosen; used as the "
    "loyalty idempotency key. Get the set from promo.show — never fabricate.",
]
Msisdn = Annotated[
    str,
    "Mobile number, 8 digits, e.g. 90000005. No country code, no spaces.",
]
Iccid = Annotated[
    str,
    "eSIM ICCID, 19-20 digits starting with 8910101, e.g. 8910101000000000005.",
]
Email = Annotated[
    str,
    "RFC5322 email address, e.g. ck@example.com.",
]
Phone = Annotated[
    str,
    "E.164 phone number with country code, e.g. +6590000005.",
]
Currency = Annotated[
    str,
    "ISO-4217 currency code. v0.1 uses SGD only.",
]
IsoDatetime = Annotated[
    str,
    "ISO-8601 datetime, e.g. 2026-04-12T13:05:00Z. Use clock.now to get current time.",
]
Duration = Annotated[
    str,
    "Human duration string. Format: Nd (days), Nh (hours), Nm (minutes), Ns (seconds). "
    "Examples: '30d', '1h', '15m', '45s'.",
]

# ─────────────────────────────────────────────────────────────────────────────
# Enum types — Literal[...] renders as JSON-schema enum
# ─────────────────────────────────────────────────────────────────────────────

# v1.1 — promotion enums
DiscountType = Literal["percent", "absolute"]
DurationKind = Literal["single", "multi", "perpetual"]
PromoAudience = Literal["public", "targeted"]
PromoCodeKind = Literal[
    "single_use_shared", "multi_use", "single_use_unique_per_customer"
]

CustomerState = Literal["pending", "active", "suspended", "closed"]
SubscriptionState = Literal["pending", "active", "blocked", "terminated"]
OrderState = Literal["acknowledged", "in_progress", "completed", "failed", "cancelled"]
ServiceOrderState = Literal["in_progress", "completed", "failed", "cancelled"]
ServiceState = Literal[
    "feasibility_checked",
    "designed",
    "reserved",
    "activated",
    "failed",
    "terminated",
]
CaseState = Literal["open", "in_progress", "pending_customer", "resolved", "closed"]
TicketState = Literal[
    "open",
    "acknowledged",
    "in_progress",
    "pending",
    "resolved",
    "closed",
    "cancelled",
]
CasePriority = Literal["low", "medium", "high", "critical"]
CaseCategory = Literal["technical", "billing", "account", "information"]
PortDirection = Literal["port_in", "port_out"]
PortRequestState = Literal["requested", "validated", "completed", "rejected"]
EscalationCategory = Literal[
    # The five non-negotiable v0.12 chat escalation categories per
    # phases/V0_12_0.md §4.4. Plus ``other`` as a catch-all the CSR
    # re-categorises during triage. Adding a sixth category is a
    # doctrine decision — the system prompt, the enum here, and the
    # case-open route encode the same list.
    "fraud",
    "billing_dispute",
    "regulator_complaint",
    "identity_recovery",
    "bereavement",
    "other",
]
TicketType = Literal[
    "service_outage",
    "billing_issue",
    "information",
    "complaint",
    "configuration_change",
    "fraud_report",
    "cancellation_request",
]
ContactMediumType = Literal["email", "mobile", "address"]
AgentState = Literal["active", "inactive"]
UsageEventType = Literal["data", "voice_minutes", "sms"]
UsageUnit = Literal["mb", "gb", "minutes", "count"]
ProvisioningTaskState = Literal[
    "pending",
    "in_progress",
    "completed",
    "failed",
    "stuck",
]
ProvisioningTaskType = Literal[
    "HLR_PROVISION",
    "HLR_DEPROVISION",
    "PCRF_POLICY_PUSH",
    "OCS_BALANCE_INIT",
    "ESIM_PROFILE_PREPARE",
]
FaultType = Literal["fail_first_attempt", "fail_always", "stuck", "slow"]
AggregateType = Literal[
    "customer",
    "subscription",
    "order",
    "service_order",
    "service",
    "case",
    "ticket",
    "payment_method",
    "provisioning_task",
]
TraceId = Annotated[
    str,
    "32-char hex W3C trace ID, e.g. 4a8f9e2c0123456789abcdef01234567. "
    "Get from trace.for_order or trace.for_subscription, or from "
    "audit.domain_event.trace_id (post-v0.2 events).",
]
