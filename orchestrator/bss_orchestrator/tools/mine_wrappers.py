"""Customer-scoped tool wrappers — the prompt-injection containment layer (v0.12).

Every ``*.mine`` / ``*_for_me`` tool here is a thin wrapper around its
canonical tool. The wrapper:

1. Reads ``customer_id`` from ``auth_context.current().actor`` — never
   from a parameter. The signature simply omits ``customer_id``;
   ``_profiles.validate_profiles()`` enforces this at startup.

2. For wrappers that accept a resource id (subscription_id, etc.),
   pre-checks ownership against the bound actor and refuses with a
   structured ``policy.<tool>.not_owned_by_actor`` error if the
   resource belongs to someone else. Server-side policies remain the
   primary boundary; the wrapper's pre-check produces a friendlier
   error and ensures the chat-surface LLM never sees the canonical
   tool's structured-error string for cross-customer attempts.

3. Calls the canonical tool internally with the actor-bound ids.

The wrappers add no new server-side capability — they narrow the
prompt-visible surface so the LLM cannot even *attempt* to act on
another customer. Server-side policies still gate every write, so a
hypothetical wrapper bug cannot widen the policy boundary.

PR2 ships the read-only wrappers below. PR3 adds the writes.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .. import auth_context
from ..clients import get_clients
from ..types import (
    CaseState,
    EscalationCategory,
    IsoDatetime,
    ProductOfferingId,
    SubscriptionId,
    UsageEventType,
    VasOfferingId,
)
from ._registry import register


class _NoActorBound(Exception):
    """Raised when a *.mine wrapper runs outside a chat-scoped invocation.

    The orchestrator's auth_context defaults to actor=None; a *.mine
    wrapper hitting that means the chat route forgot to call
    ``astream_once(actor=customer_id, ...)``. Surfaced to the LLM as a
    structured error string by the standard tool-error handler.
    """


def _require_actor() -> str:
    """Return the bound customer_id or raise.

    The exception is converted to a structured ``CHAT_NO_ACTOR_BOUND``
    error string by the graph's tool-error wrapper, so the LLM sees a
    clean observation rather than a stack trace.
    """
    actor = auth_context.current().actor
    if not actor:
        raise _NoActorBound(
            "chat.no_actor_bound: this tool can only run inside a "
            "customer-scoped chat session"
        )
    return actor


async def _assert_subscription_owned(subscription_id: str, actor: str) -> dict[str, Any]:
    """Fetch the subscription and confirm it belongs to ``actor``.

    Returns the subscription dict on success. Raises a structured
    ownership error if the subscription's customer_id does not match
    the bound actor. Used by every subscription-scoped *.mine wrapper
    so cross-customer attempts produce a clean, identical error
    irrespective of which tool the LLM tried.
    """
    sub = await get_clients().subscription.get(subscription_id)
    sub_customer_id = sub.get("customerId") or sub.get("customer_id")
    if sub_customer_id != actor:
        raise _NotOwnedByActor(
            f"policy.subscription.not_owned_by_actor: subscription "
            f"{subscription_id} is not yours"
        )
    return sub


class _NotOwnedByActor(Exception):
    """Cross-customer access attempt blocked at the wrapper.

    The policy layer would catch the same case server-side; raising
    here keeps the error shape and message uniform across reads and
    writes, so the agent's behaviour to a prompt-injection attempt is
    always the same observation — never a leaked subscription dict.
    """


# ─── Subscription reads ──────────────────────────────────────────────


def _discount_label(dtype: str, dval: Any, currency: str) -> str:
    """Human label for a promo discount — mirrors bss_models.discount_label
    (inlined to keep the orchestrator dep-free of bss-models)."""
    from decimal import Decimal

    v = Decimal(str(dval))
    if dtype == "percent":
        return f"{format(v.normalize(), 'f')}% off"
    return f"{currency} {v.quantize(Decimal('0.01'))} off"


def _annotate_pricing(sub: dict[str, Any]) -> dict[str, Any]:
    """Surface the *current* monthly charge + an active-discount note on a
    subscription dict the chat sees, so the LLM reports what the customer
    actually pays (effective price) — not the base list price — when a promo is
    live. Code-enforced at the seam: the prompt alone isn't reliable on small
    models (see [[feedback_llm_hallucination_fight]])."""
    price = sub.get("priceAmount")
    effective = sub.get("effectiveAmount")
    currency = sub.get("priceCurrency") or "SGD"
    dtype = sub.get("discountType")
    dval = sub.get("discountValue")
    remaining = sub.get("discountPeriodsRemaining", 0) or 0
    current = effective if effective is not None else price
    sub["currentMonthlyCharge"] = (
        f"{currency} {current}" if current is not None else None
    )
    if dtype and dval is not None and remaining != 0:
        label = _discount_label(dtype, dval, currency)
        if remaining == -1:
            sub["activeDiscount"] = f"{label} (ongoing)"
        else:
            plural = "" if remaining == 1 else "s"
            sub["activeDiscount"] = (
                f"{label} — {remaining} more renewal{plural} at this price, "
                f"then {currency} {price}/mo"
            )
    else:
        sub["activeDiscount"] = None
    return sub


@register("subscription.list_mine")
async def subscription_list_mine() -> list[dict[str, Any]]:
    """List the logged-in customer's subscriptions. customer_id is
    bound from the chat session — never accepted as a parameter.

    Args:
        (none — customer_id is bound from auth_context.current().actor.)

    Returns:
        List of subscription summary dicts owned by the actor. Empty
        if the customer has no lines.

    Raises:
        chat.no_actor_bound: invoked outside a chat-scoped session.
    """
    actor = _require_actor()
    subs = await get_clients().subscription.list_for_customer(actor)
    return [_annotate_pricing(s) for s in subs]


@register("subscription.get_mine")
async def subscription_get_mine(subscription_id: SubscriptionId) -> dict[str, Any]:
    """Read one of the logged-in customer's subscriptions in full.

    Args:
        subscription_id: the actor-owned subscription id (opaque suffix). Cross-customer
            attempts return ``policy.subscription.not_owned_by_actor``.

    Returns:
        Subscription dict ``{id, customerId, offeringId, msisdn,
        iccid, state, balances, nextRenewalAt}``.

    Raises:
        policy.subscription.not_owned_by_actor: subscription belongs
            to another customer.
        chat.no_actor_bound: invoked outside a chat-scoped session.
    """
    actor = _require_actor()
    return _annotate_pricing(await _assert_subscription_owned(subscription_id, actor))


@register("subscription.get_balance_mine")
async def subscription_get_balance_mine(
    subscription_id: SubscriptionId,
) -> dict[str, Any]:
    """Read bundle balances for one of the logged-in customer's
    subscriptions. Use before offering a VAS top-up — the user wants
    to see ``used / total`` for each resource.

    Args:
        subscription_id: the actor-owned subscription id (opaque suffix).

    Returns:
        Balance dict ``{subscriptionId, balances: [{type, used, total,
        unit}]}``.

    Raises:
        policy.subscription.not_owned_by_actor: subscription belongs
            to another customer.
        chat.no_actor_bound: invoked outside a chat-scoped session.
    """
    actor = _require_actor()
    await _assert_subscription_owned(subscription_id, actor)
    return await get_clients().subscription.get_balance(subscription_id)


@register("subscription.get_lpa_mine")
async def subscription_get_lpa_mine(
    subscription_id: SubscriptionId,
) -> dict[str, Any]:
    """Return the LPA activation-code bundle for one of the logged-in
    customer's eSIMs. Use when the customer asks "how do I redownload
    my eSIM?" — the bundle includes the activation code and QR
    payload.

    Args:
        subscription_id: the actor-owned subscription id (opaque suffix).

    Returns:
        ``{subscriptionId, iccid, imsi, activationCode, msisdn}``.
        ``activationCode`` is the LPA string (``LPA:1$...``).

    Raises:
        policy.subscription.not_owned_by_actor: subscription belongs
            to another customer.
        chat.no_actor_bound: invoked outside a chat-scoped session.
        NotFound: no eSIM attached to this subscription.
    """
    actor = _require_actor()
    await _assert_subscription_owned(subscription_id, actor)
    return await get_clients().subscription.get_esim_activation(subscription_id)


# ─── Usage reads ─────────────────────────────────────────────────────


@register("usage.history_mine")
async def usage_history_mine(
    subscription_id: SubscriptionId | None = None,
    event_type: UsageEventType | None = None,
    since: IsoDatetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read recent usage for the logged-in customer, optionally
    narrowed to one of their subscriptions.

    Args:
        subscription_id: Optional subscription id (SUB-...) to narrow the query to one
            of the customer's lines. Cross-customer attempts return
            ``policy.subscription.not_owned_by_actor``. When omitted,
            usage across every line owned by the customer is returned.
        event_type: Optional ``data`` / ``voice_minutes`` / ``sms``
            filter.
        since: Optional ISO-8601 lower bound.
        limit: Max rows (default 100, server cap 1000).

    Returns:
        List of usage dicts, newest first.

    Raises:
        policy.subscription.not_owned_by_actor: ``subscription_id``
            belongs to another customer.
        chat.no_actor_bound: invoked outside a chat-scoped session.
    """
    actor = _require_actor()
    if subscription_id is not None:
        await _assert_subscription_owned(subscription_id, actor)
        return await get_clients().mediation.list_usage(
            subscription_id=subscription_id,
            event_type=event_type,
            since=since,
            limit=limit,
        )
    # No specific subscription — fan out across the actor's lines and
    # merge. Server-side filtering by customer would be ideal but the
    # mediation API takes one subscription/msisdn at a time. v0.12
    # accepts the multi-call cost; v1.x can add a customer-scoped
    # endpoint if usage becomes a hot path.
    subs = await get_clients().subscription.list_for_customer(actor)
    results: list[dict[str, Any]] = []
    for sub in subs:
        sub_id = sub.get("id")
        if not sub_id:
            continue
        rows = await get_clients().mediation.list_usage(
            subscription_id=sub_id,
            event_type=event_type,
            since=since,
            limit=limit,
        )
        results.extend(rows)
    # Newest-first, capped at the requested limit.
    results.sort(key=lambda r: r.get("eventTime") or r.get("event_time") or "", reverse=True)
    return results[:limit]


# ─── Customer + payment reads ────────────────────────────────────────


@register("customer.get_mine")
async def customer_get_mine() -> dict[str, Any]:
    """Read the logged-in customer's record (contact mediums, KYC
    status, account state). customer_id is bound from the chat
    session — no parameter.

    Args:
        (none — customer_id is bound from auth_context.current().actor.)

    Returns:
        Customer dict including ``status`` (pending/active/suspended/
        closed), ``contactMedium`` list, ``kycVerified`` boolean, and
        timestamps.

    Raises:
        chat.no_actor_bound: invoked outside a chat-scoped session.
    """
    actor = _require_actor()
    return await get_clients().crm.get_customer(actor)


@register("payment.method_list_mine")
async def payment_method_list_mine() -> list[dict[str, Any]]:
    """List the logged-in customer's cards on file.

    Args:
        (none — customer_id is bound from auth_context.current().actor.)

    Returns:
        List of payment method dicts ``{id, customerId, brand, last4,
        expMonth, expYear, isDefault}``. Only the actor's methods.

    Raises:
        chat.no_actor_bound: invoked outside a chat-scoped session.
    """
    actor = _require_actor()
    return await get_clients().payment.list_methods(actor)


@register("payment.charge_history_mine")
async def payment_charge_history_mine(
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List recent payment attempts for the logged-in customer.

    Args:
        limit: Max rows (default 20, server cap applies).

    Returns:
        List of payment attempt dicts, newest first. Only the
        actor's attempts.

    Raises:
        chat.no_actor_bound: invoked outside a chat-scoped session.
    """
    actor = _require_actor()
    return await get_clients().payment.list_payments(
        customer_id=actor, limit=limit
    )


# ─── Writes (PR3) ────────────────────────────────────────────────────


@register("vas.purchase_for_me")
async def vas_purchase_for_me(
    subscription_id: SubscriptionId,
    vas_offering_id: VasOfferingId,
) -> dict[str, Any]:
    """Purchase a VAS top-up for one of the logged-in customer's
    subscriptions. Charges the customer's default card on file.

    Use this when the customer asks to top up data / minutes / SMS,
    or to recover from a blocked-on-exhaust line. Pick the VAS id
    from ``catalog.list_vas`` — never invent one.

    Args:
        subscription_id: the actor-owned subscription id (opaque suffix).
        vas_offering_id: VAS offering id (e.g. ``VAS_DATA_5GB``)
            from ``catalog.list_vas``.

    Returns:
        Updated subscription dict with the refreshed balances. If the
        line was ``blocked``, expect ``state="active"`` now.

    Raises:
        policy.subscription.not_owned_by_actor: subscription belongs
            to another customer.
        chat.no_actor_bound: invoked outside a chat-scoped session.
        PolicyViolationFromServer: e.g.
            ``subscription.vas_purchase.requires_active_cof`` (no
            valid card on file — the customer must add one in the
            payment-methods page first).
    """
    actor = _require_actor()
    await _assert_subscription_owned(subscription_id, actor)
    return await get_clients().subscription.purchase_vas(
        subscription_id, vas_offering_id
    )


@register("subscription.schedule_plan_change_mine")
async def subscription_schedule_plan_change_mine(
    subscription_id: SubscriptionId,
    new_offering_id: ProductOfferingId,
) -> dict[str, Any]:
    """Schedule a plan change on one of the logged-in customer's
    subscriptions. Applies at the next renewal — **no proration**.

    The new offering's price is snapshotted now; renewal day charges
    the snapshot regardless of catalog moves in between. Tell the
    customer the renewal date and that the current plan continues
    until then.

    Args:
        subscription_id: the actor-owned subscription id (opaque suffix).
        new_offering_id: Target offering (e.g. ``PLAN_L``). Must be
            in the active catalog and different from the current
            plan.

    Returns:
        Updated subscription dict with ``pendingOfferingId`` /
        ``pendingOfferingPriceId`` / ``pendingEffectiveAt`` populated.

    Raises:
        policy.subscription.not_owned_by_actor: subscription belongs
            to another customer.
        chat.no_actor_bound: invoked outside a chat-scoped session.
        PolicyViolationFromServer: e.g.
            ``subscription.plan_change.same_offering`` /
            ``subscription.plan_change.already_pending`` /
            ``subscription.plan_change.target_not_sellable_now``.
    """
    actor = _require_actor()
    await _assert_subscription_owned(subscription_id, actor)
    return await get_clients().subscription.schedule_plan_change(
        subscription_id, new_offering_id
    )


@register("subscription.cancel_pending_plan_change_mine")
async def subscription_cancel_pending_plan_change_mine(
    subscription_id: SubscriptionId,
) -> dict[str, Any]:
    """Cancel a previously-scheduled plan change on one of the
    logged-in customer's subscriptions. Idempotent — no-op if there
    is nothing pending.

    Args:
        subscription_id: the actor-owned subscription id (opaque suffix).

    Returns:
        Updated subscription dict with the pending fields cleared.

    Raises:
        policy.subscription.not_owned_by_actor: subscription belongs
            to another customer.
        chat.no_actor_bound: invoked outside a chat-scoped session.
    """
    actor = _require_actor()
    await _assert_subscription_owned(subscription_id, actor)
    return await get_clients().subscription.cancel_plan_change(subscription_id)


@register("subscription.terminate_mine")
async def subscription_terminate_mine(
    subscription_id: SubscriptionId,
) -> dict[str, Any]:
    """**DESTRUCTIVE — releases the MSISDN + eSIM, no undo.** Gated
    by ``safety.py``. Terminate one of the logged-in customer's
    own subscriptions. Used only when the customer explicitly asks
    to cancel a specific line by name.

    Never call as a "fix" for a blocked / exhausted subscription;
    for that, use ``vas.purchase_for_me`` (non-destructive, adds
    allowance).

    The ``reason`` carried into the audit trail is
    ``"customer_chat"`` so subsequent investigation distinguishes
    chat-driven terminations from CSR / scenario / API ones.

    Args:
        subscription_id: the actor-owned subscription id (opaque suffix).

    Returns:
        Updated subscription dict with ``state="terminated"``.

    Raises:
        policy.subscription.not_owned_by_actor: subscription belongs
            to another customer.
        chat.no_actor_bound: invoked outside a chat-scoped session.
        PolicyViolationFromServer:
            ``subscription.terminate.already_terminated``.
    """
    actor = _require_actor()
    await _assert_subscription_owned(subscription_id, actor)
    return await get_clients().subscription.terminate(
        subscription_id, reason="customer_chat"
    )


# ─── Escalation (PR6) ────────────────────────────────────────────────


# Map the v0.12 EscalationCategory enum to the existing CRM
# CaseCategory taxonomy so cases land in the right CSR queue.
_ESCALATION_TO_CASE_CATEGORY: dict[str, str] = {
    "fraud": "account",
    "billing_dispute": "billing",
    "regulator_complaint": "account",
    "identity_recovery": "account",
    "bereavement": "account",
    "other": "information",
}

# Default priority per escalation category. Fraud / regulator /
# identity-recovery skip the queue; bereavement gets human attention
# but isn't an emergency; billing disputes default to medium and
# CSRs adjust on triage.
_ESCALATION_TO_PRIORITY: dict[str, str] = {
    "fraud": "high",
    "billing_dispute": "medium",
    "regulator_complaint": "high",
    "identity_recovery": "high",
    "bereavement": "medium",
    "other": "medium",
}


@register("case.list_for_me")
async def case_list_for_me(
    state: CaseState | None = None,
) -> list[dict[str, Any]]:
    """List cases the logged-in customer has on file with us.

    Use this when the customer asks "what's my case ID?", "is my case
    still open?", "why was my case closed?", or any similar follow-up
    after a prior escalation. The wrapper binds ``customer_id`` from
    ``auth_context.current().actor``; the LLM signature must NOT carry
    a ``customer_id`` parameter (the v0.12 prompt-injection containment
    seam).

    Args:
        state: Optional filter — ``"open"`` / ``"in_progress"`` /
            ``"resolved"`` / ``"closed"``. Default returns every case
            for this customer regardless of state.

    Returns:
        List of case dicts. **All fields are customer-facing** — pass
        them through plainly when the customer asks. No "internal
        only" classification exists in v0.13. Each dict includes:

        - ``id``                — opaque case id (CASE-...)
        - ``subject``           — the case headline
        - ``state``             — open / in_progress / pending_customer
                                  / resolved / closed
        - ``category``          — e.g. ``billing_dispute``, ``fraud``
        - ``priority``          — high / medium / low
        - ``resolution_code``   — closure reason slug (e.g. ``fixed``,
                                  ``duplicate``). Tell the customer
                                  in plain English ("we closed it as
                                  fixed").
        - ``opened_at`` / ``closed_at`` — ISO timestamps.
        - ``notes``             — list of ``{body, author_agent_id,
                                  created_at}``. These are NOTES THE
                                  CUSTOMER CAN SEE — there is no
                                  internal/external distinction in
                                  v0.13. Quote them when asked why a
                                  case was closed; if a note is
                                  CSR-jargon-y ("non issue"), you may
                                  paraphrase, but don't pretend it
                                  doesn't exist.
        - ``chatTranscriptHash`` — present when the case was opened
                                   from this chat surface. Don't
                                   surface the hash itself; it's an
                                   audit pointer.

        Empty list when the customer has no cases — render plainly
        ("no cases on file").

    Raises:
        chat.no_actor_bound: invoked outside a chat-scoped session.
    """
    actor = _require_actor()
    return await get_clients().crm.list_cases(
        customer_id=actor, state=state
    )


@register("case.open_for_me")
async def case_open_for_me(
    category: EscalationCategory,
    subject: str,
    description: str,
) -> dict[str, Any]:
    """Open a case on the logged-in customer's behalf for an out-of-
    scope escalation. Use this when the conversation hits one of the
    five non-negotiable categories the AI must not attempt to resolve
    alone:

    - ``fraud`` — unauthorised charges, account takeover suspicion,
      stolen card.
    - ``billing_dispute`` — customer disputes a charge or refund
      decision.
    - ``regulator_complaint`` — IMDA / regulatory inquiry / formal
      complaint.
    - ``identity_recovery`` — account access lost; cannot prove
      identity through the standard self-serve flow.
    - ``bereavement`` — customer is calling on behalf of a deceased
      account holder.

    ``other`` is a CSR-triaged catch-all; CSRs re-categorise on
    review. Do not invent a sixth category.

    The full chat transcript up to and including this turn is hashed
    (SHA-256) and persisted via ``crm.store_chat_transcript`` before
    the case is opened. The case row carries the hash so a CSR can
    retrieve the conversation via ``case.show_transcript_for``.

    After calling this tool, tell the customer plainly: "I've opened
    a case for this. A member of our team will follow up via email
    at <email>." Do NOT promise a specific turnaround time or hour
    count — case-update emails are operator-driven today, not
    platform-automated. Do NOT attempt the resolution yourself.

    Args:
        category: One of the five non-negotiable categories or
            ``other`` for ambiguous escalations.
        subject: One-line case subject. Concrete, e.g.
            ``"Disputes charge on 2026-04-25 — claims unauthorised"``.
        description: One-paragraph free text. Capture what the
            customer said and any relevant context. The transcript
            link gives the CSR the full conversation.

    Returns:
        Case dict from ``crm.open_case``, including the new case id
        and ``chatTranscriptHash`` field. Render the id back to the
        customer so they can quote it if calling in.

    Raises:
        chat.no_actor_bound: invoked outside a chat-scoped session.
        PolicyViolationFromServer:
            ``case.open.customer_must_be_active``: customer record is
            in pending/closed state — escalate via a different path.
    """
    actor = _require_actor()
    transcript = auth_context.current().transcript or ""
    transcript_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()

    # Persist the transcript first; the case carries its hash and
    # store_chat_transcript is idempotent so a retry on transient
    # error doesn't double-write.
    await get_clients().crm.store_chat_transcript(
        hash_=transcript_hash,
        customer_id=actor,
        body=transcript,
    )

    return await get_clients().crm.open_case(
        customer_id=actor,
        subject=subject,
        category=_ESCALATION_TO_CASE_CATEGORY[category],
        priority=_ESCALATION_TO_PRIORITY[category],
        description=f"[{category}] {description}",
        chat_transcript_hash=transcript_hash,
    )
