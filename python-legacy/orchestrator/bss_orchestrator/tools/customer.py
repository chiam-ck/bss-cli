"""Customer, KYC, and interaction tools — CRM (TMF629, TMF683) + `/crm-api/v1`.

All tools are thin wrappers over ``bss_clients.CRMClient``. The LLM sees
docstrings + typed aliases from ``types.py`` and should never need to
touch raw HTTP details.
"""

from __future__ import annotations

from typing import Any

from ..clients import get_clients
from ..types import (
    ContactMediumId,
    ContactMediumType,
    CustomerId,
    CustomerState,
    Email,
    Msisdn,
    Phone,
)
from ._registry import register


@register("customer.create")
async def customer_create(
    name: str,
    email: Email | None = None,
    phone: Phone | None = None,
) -> dict[str, Any]:
    """Create a new customer record. Use this as the FIRST step of any signup
    workflow — before ``payment.add_card`` and ``order.create``. The customer
    will be created in ``pending`` state; a KYC attestation + COF are required
    before an order can be placed.

    Args:
        name: Customer display name (free text, e.g. ``"Ck"``).
        email: RFC-5322 email address, e.g. ``"ck@example.com"``. Optional but
            strongly recommended — required by some downstream policies.
        phone: E.164 phone number with country code, e.g. ``"+6590000005"``.

    Returns:
        The created customer dict with ``id`` (CUST-... opaque suffix), ``name``, ``status``,
        and a ``contactMedium`` list. Pass ``id`` to subsequent tools.

    Raises:
        PolicyViolationFromServer: common rules:
            - ``customer.create.email_unique``: email already belongs to another
              customer. Ask the user whether they meant to look up the existing
              customer via ``customer.list``.
            - ``customer.create.invalid_email``: format is invalid.
    """
    return await get_clients().crm.create_customer(name=name, email=email, phone=phone)


@register("customer.find_by_msisdn")
async def customer_find_by_msisdn(msisdn: Msisdn) -> dict[str, Any]:
    """Find the customer who owns a given mobile number.

    Use when the operator types a phone number in the search box, or
    when a question like *"who's calling from +6590001234?"* needs an
    ID for subsequent ``customer.get`` / ``subscription.list_for_customer``
    calls. Resolves MSISDN → subscription → customer in one hop.

    Args:
        msisdn: 8+ digit mobile number, no formatting (e.g.,
            ``"90001234"``). Country code is optional and stripped to
            the local digits in the inventory pool.

    Returns:
        Customer dict — same shape as ``customer.get``.

    Raises:
        NotFound: number is unassigned, or the owning subscription's
            customer was deleted.
    """
    return await get_clients().crm.find_customer_by_msisdn(msisdn)


@register("customer.find_by_email")
async def customer_find_by_email(email: Email) -> dict[str, Any]:
    """Find the customer whose contact medium carries a given email
    address. Use this FIRST when the operator identifies a customer by
    email (e.g. *"escalation from jane+1@example.com"*) — do NOT pass an
    email to ``customer.list``'s ``name_contains``; that filter matches
    the display name only and will return ``[]`` for every email.

    Args:
        email: Exact email address to resolve, e.g.
            ``"jane+1@example.com"``. Case-sensitive exact match on an
            active email contact medium — no substring or fuzzy match.

    Returns:
        Customer dict — same shape as ``customer.get`` minus the
        ``_extras`` composite. Pass ``id`` to ``customer.get`` for the
        full 360 view.

    Raises:
        NotFound: no customer has this email. Tell the operator the
            address is unknown — do not fall back to ``customer.list``
            with the email as a name filter.
    """
    return await get_clients().crm.find_customer_by_email(email)


@register("customer.get")
async def customer_get(customer_id: CustomerId) -> dict[str, Any]:
    """Read a single customer with contact mediums, KYC status, and the
    adjacent context the cockpit's 360 renderer expects (active
    subscriptions, open cases, recent interactions).

    The CRM API only returns the customer record itself; the cockpit's
    deterministic ASCII renderer needs subscriptions / cases /
    interactions to fill the "Subscriptions (N)", "Open Cases (N)",
    "Recent Interactions (N)" sections. Without them the card showed
    zeros even when the customer had real subs and cases — see
    cockpit transcript May 2026. We compose the joins here so the
    rendered card is honest, and so any caller of ``customer.get``
    (cockpit, REPL, scenarios) gets the same shape.

    The four upstream calls run in parallel — they are independent
    reads against three services (CRM for customer + cases +
    interactions, Subscription for the line list). Total latency is
    bounded by the slowest read, not their sum.

    The composite extras are surfaced under the synthetic ``_extras``
    key so the customer dict's TMF629 shape is unchanged for callers
    that only want the core record. ``_extras`` is the renderer's
    plumbing channel; consumers that care only about the customer
    fields should ignore it.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix). Obtain from
            ``customer.list`` or ``customer.create``.

    Returns:
        Customer dict including ``status`` (pending/active/suspended/closed),
        ``contactMedium`` list, ``kycVerified`` boolean, timestamps, plus an
        ``_extras`` key containing ``{"subscriptions": [...], "cases": [...],
        "interactions": [...]}``.

    Raises:
        NotFound: no customer with this ID.
    """
    import asyncio as _asyncio

    clients = get_clients()
    customer, subscriptions, cases, interactions = await _asyncio.gather(
        clients.crm.get_customer(customer_id),
        clients.subscription.list_for_customer(customer_id),
        clients.crm.list_cases(customer_id=customer_id),
        clients.crm.list_interactions(customer_id, limit=10),
        # Sub-domain reads should never wedge the 360 — if any one
        # fails (e.g. subscription service is down), surface the
        # customer record alone with empty extras rather than
        # erroring the entire customer.get tool.
        return_exceptions=True,
    )
    # ``customer`` failure IS a hard error — that's the primary
    # record. Re-raise so the caller sees a real NotFound.
    if isinstance(customer, BaseException):
        raise customer
    customer["_extras"] = {
        "subscriptions": subscriptions if isinstance(subscriptions, list) else [],
        "cases": cases if isinstance(cases, list) else [],
        "interactions": interactions if isinstance(interactions, list) else [],
    }
    return customer


@register("customer.list")
async def customer_list(
    state: CustomerState | None = None,
    name_contains: str | None = None,
) -> list[dict[str, Any]]:
    """List customers, optionally filtered. Use this when the user refers to
    a customer by name (e.g. "Ck") — filter by ``name_contains`` to resolve
    the CUST-NNN ID before calling other tools. For an email address use
    ``customer.find_by_email``; for a phone number use
    ``customer.find_by_msisdn`` — ``name_contains`` matches neither.

    Args:
        state: Optional customer state filter.
        name_contains: Optional case-insensitive substring match on the
            customer's display NAME only — emails and phone numbers
            never match.

    Returns:
        List of customer dicts (may be empty). Each has ``id``, ``name``,
        ``status``. If multiple match, ask the user which one — do not guess.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().crm.list_customers(state=state, name_contains=name_contains)


@register("customer.add_contact_medium")
async def customer_add_contact_medium(
    customer_id: CustomerId,
    medium_type: ContactMediumType,
    value: str,
) -> dict[str, Any]:
    """Add an additional contact medium (email/mobile/address) to a customer.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).
        medium_type: One of ``email``, ``mobile``, ``address``.
        value: The contact value itself — email string, E.164 phone, or
            address text, matching ``medium_type``.

    Returns:
        The updated customer dict with the new contact medium appended.

    Raises:
        PolicyViolationFromServer: ``customer.contact_medium.format_invalid``.
    """
    return await get_clients().crm.add_contact_medium(
        customer_id, medium_type=medium_type, value=value
    )


@register("customer.update_contact")
async def customer_update_contact(
    customer_id: CustomerId,
    email: Email | None = None,
    phone: Phone | None = None,
) -> dict[str, Any]:
    """Update primary email/phone on a customer. Only non-None fields are
    patched — None fields are left untouched.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).
        email: New primary email (optional).
        phone: New primary phone (optional).

    Returns:
        The updated customer dict.

    Raises:
        PolicyViolationFromServer: ``customer.update.email_unique``.
    """
    patch: dict[str, Any] = {}
    if email is not None:
        patch["email"] = email
    if phone is not None:
        patch["phone"] = phone
    return await get_clients().crm.update_customer(customer_id, patch)


@register("customer.remove_contact_medium")
async def customer_remove_contact_medium(
    customer_id: CustomerId,
    medium_id: ContactMediumId,
) -> dict[str, Any]:
    """Remove a specific contact medium from a customer. DESTRUCTIVE — the
    ``safety.py`` wrapper will block this without ``--allow-destructive``.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).
        medium_id: Contact Medium ID with the CM- prefix (opaque suffix). Obtain via ``customer.get``.

    Returns:
        ``{"id": "CM-NNN", "removed": true}`` on success.

    Raises:
        PolicyViolationFromServer:
            - ``customer.contact_medium.last_remaining``: can't remove the
              customer's only contact medium.
    """
    return await get_clients().crm.remove_contact_medium(customer_id, medium_id)


@register("customer.close")
async def customer_close(customer_id: CustomerId) -> dict[str, Any]:
    """Close a customer account. DESTRUCTIVE — gated by ``safety.py``.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).

    Returns:
        The updated customer dict with ``status="closed"``.

    Raises:
        PolicyViolationFromServer:
            - ``customer.close.no_active_subscriptions``: terminate the
              customer's subscriptions first (also destructive).
    """
    return await get_clients().crm.close_customer(customer_id)


# ── KYC ────────────────────────────────────────────────────────────────

@register("customer.attest_kyc")
async def customer_attest_kyc(
    customer_id: CustomerId,
    provider: str,
    attestation_token: str,
) -> dict[str, Any]:
    """Record a signed KYC attestation from the channel layer. BSS-CLI does
    NOT run eKYC itself — the mobile app / web portal completes the vendor
    flow and passes the signed token here. The attestation unlocks
    ``order.create``.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).
        provider: Attestation provider slug. v0.15+ ships two: ``"didit"``
            (real production via Didit's hosted UI + signed-webhook
            corroboration) and ``"prebaked"`` (dev / scenarios — stub
            attestation accepted only when ``BSS_KYC_ALLOW_PREBAKED=true``
            outside production). Other vendors (Singpass, Jumio, Onfido)
            would integrate at the channel layer with their own slug.
        attestation_token: Signed JWT from the vendor. Never fabricate.

    Returns:
        ``{"customerId", "provider", "status", "verifiedAt"}``.

    Raises:
        PolicyViolationFromServer:
            - ``customer.attest_kyc.signature_invalid``: token failed verification.
    """
    return await get_clients().crm.attest_kyc(
        customer_id, provider=provider, attestation_token=attestation_token
    )


@register("customer.get_kyc_status")
async def customer_get_kyc_status(customer_id: CustomerId) -> dict[str, Any]:
    """Return the current KYC state + expiry for a customer.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).

    Returns:
        ``{"state": "verified"|"unverified"|"expired", "verifiedAt", "expiresAt"}``.

    Raises:
        NotFound: no customer with this ID.
    """
    return await get_clients().crm.get_kyc_status(customer_id)


# ── Interactions ───────────────────────────────────────────────────────

@register("interaction.log")
async def interaction_log(
    customer_id: CustomerId,
    summary: str,
    body: str | None = None,
) -> dict[str, Any]:
    """**Always call this at the end of a troubleshoot or fix.** Log a
    customer interaction note (TMF683) so the next CSR opening this
    customer's record sees what already happened. Skipping this leaves
    them flying blind — it is the close of the work, not a follow-up.

    Call exactly once per troubleshoot, AFTER the fix tool succeeded
    (e.g. after ``subscription.purchase_vas`` returned a successful
    response). The summary is the one-line story of what you did and
    what the outcome was — no more, no less.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).
        summary: One-line summary of the interaction (required). Example:
            ``"Purchased VAS_DATA_1GB — subscription SUB-007 unblocked."``
        body: Optional longer free text (diagnosis steps, cited IDs, etc.).

    Returns:
        The created interaction dict. ``channel`` is filled from the
        request context server-side, so callers don't pass it.

    Raises:
        (none expected)
    """
    return await get_clients().crm.log_interaction(
        customer_id=customer_id, summary=summary, body_text=body
    )


@register("interaction.list")
async def interaction_list(
    customer_id: CustomerId,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read the interaction log for a customer, newest first.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).
        limit: Max rows to return (default 50).

    Returns:
        List of interaction dicts ``{id, createdAt, channel, actor, action, note}``.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().crm.list_interactions(customer_id, limit=limit)
