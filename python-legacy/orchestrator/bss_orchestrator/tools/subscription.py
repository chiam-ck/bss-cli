"""Subscription tools — balance, VAS, renewal, termination, eSIM activation."""

from __future__ import annotations

from typing import Any

from ..clients import get_clients
from ..types import (
    CustomerId,
    IsoDatetime,
    ProductOfferingId,
    ProductOfferingPriceId,
    SubscriptionId,
    VasOfferingId,
)
from ._registry import register


@register("subscription.get")
async def subscription_get(subscription_id: SubscriptionId) -> dict[str, Any]:
    """Read a subscription with bundle balances + renewal info. This is the
    go-to diagnostic read — the ``state`` field tells you whether the service
    is active, blocked, or terminated.

    Args:
        subscription_id: Subscription ID with the SUB- prefix (opaque suffix).

    Returns:
        Subscription dict ``{id, customerId, offeringId, msisdn, iccid,
        state, balances, nextRenewalAt}``. ``balances`` is a list of
        ``{type, used, total, unit}`` rows — ``total=None`` means unlimited.

    Raises:
        NotFound: unknown subscription.
    """
    return await get_clients().subscription.get(subscription_id)


@register("subscription.list_for_customer")
async def subscription_list_for_customer(
    customer_id: CustomerId,
) -> list[dict[str, Any]]:
    """List a customer's subscriptions.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).

    Returns:
        List of subscription summary dicts. Call ``subscription.get`` for full detail.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().subscription.list_for_customer(customer_id)


@register("subscription.get_balance")
async def subscription_get_balance(subscription_id: SubscriptionId) -> dict[str, Any]:
    """Read bundle balances for a subscription. Use this before offering a
    VAS top-up — the user wants to know ``used / total`` for each resource.

    Args:
        subscription_id: Subscription ID with the SUB- prefix (opaque suffix).

    Returns:
        Balance dict ``{subscriptionId, balances: [{type, used, total, unit}]}``.

    Raises:
        NotFound: unknown subscription.
    """
    return await get_clients().subscription.get_balance(subscription_id)


@register("subscription.purchase_vas")
async def subscription_purchase_vas(
    subscription_id: SubscriptionId,
    vas_offering_id: VasOfferingId,
) -> dict[str, Any]:
    """**Non-destructive.** Purchase a VAS (Value-Added Service) for a
    subscription and charge the customer's default card-on-file.

    **This is the canonical fix for a blocked-on-exhaust subscription.**
    When a customer reports "data isn't working" / "I'm blocked" and
    you've confirmed ``state == "blocked"``, the recovery is to call
    this tool with a data VAS from ``catalog.list_vas``. Do not refuse
    on a destructive-action caveat — VAS top-up adds allowance, it
    never removes service. The opposite tool — ``subscription.terminate``
    — IS destructive and is NOT what to call here.

    Use cases: blocked-on-exhaust recovery (primary), or proactive
    allowance top-up on an active subscription.

    Args:
        subscription_id: Subscription ID with the SUB- prefix (opaque suffix).
        vas_offering_id: VAS offering ID (e.g. ``VAS_DATA_5GB``). Get this
            from ``catalog.list_vas`` — do not guess.

    Returns:
        Updated subscription dict. If the subscription was ``blocked``, expect
        it to be ``active`` now. Check ``balances`` for the updated allowance.
        After this returns, call ``interaction.log`` with a one-line summary
        to close out the troubleshoot.

    Raises:
        PolicyViolationFromServer:
            - ``subscription.vas_purchase.requires_active_cof``: customer has
              no active card. Fix with ``payment.add_card``, then retry.
            - ``subscription.vas_purchase.vas_offering_sellable``: VAS offering
              is inactive. Ask ``catalog.list_vas`` for valid IDs.
            - ``subscription.vas_purchase.not_if_terminated``: subscription is
              terminated; not recoverable.
    """
    return await get_clients().subscription.purchase_vas(
        subscription_id, vas_offering_id
    )


@register("subscription.terminate")
async def subscription_terminate(subscription_id: SubscriptionId) -> dict[str, Any]:
    """**DESTRUCTIVE — releases MSISDN + eSIM, no undo.** Gated by
    ``safety.py``. Only call this when the user explicitly asked to
    terminate THIS subscription by name. Never use as a "fix" for a
    blocked / exhausted / failing subscription — for those, the right
    tool is ``subscription.purchase_vas`` (non-destructive, adds
    allowance, unblocks).

    Effects: subscription state → ``terminated``, eSIM profile released
    back to inventory, MSISDN released back to inventory, customer's
    line is gone. Cannot be reversed; the customer would have to sign
    up again and would NOT get the same number back.

    Args:
        subscription_id: Subscription ID with the SUB- prefix (opaque suffix).

    Returns:
        Updated subscription dict with ``state="terminated"``.

    Raises:
        PolicyViolationFromServer:
            - ``subscription.terminate.already_terminated``.
    """
    return await get_clients().subscription.terminate(subscription_id)


@register("subscription.renew_now")
async def subscription_renew_now(subscription_id: SubscriptionId) -> dict[str, Any]:
    """Manually trigger a subscription renewal (charges COF, resets balances).
    Normal renewals happen automatically on the period boundary via the
    v0.18 in-process worker — use this only for edge cases (operator
    reproducing a charge, scenario driving a deterministic outcome).

    Args:
        subscription_id: Subscription ID with the SUB- prefix (opaque suffix).

    Returns:
        Updated subscription dict with fresh ``balances`` and new ``nextRenewalAt``.

    Raises:
        PolicyViolationFromServer:
            - ``subscription.renew.requires_active_cof``: no valid card; fix
              with ``payment.add_card`` then retry.
    """
    return await get_clients().subscription.renew(subscription_id)


@register("subscription.tick_renewals_now")
async def subscription_tick_renewals_now() -> dict[str, Any]:
    """Run one deterministic renewal sweep (v0.18 admin endpoint).

    Invokes the same active-due + blocked-overdue sweep the in-process
    worker runs every ``BSS_RENEWAL_TICK_SECONDS``. Used by scenarios
    after ``clock.advance`` so they don't wait for the wall-clock
    interval. Operator-only (the tool wraps an endpoint gated by
    ``BSS_ALLOW_ADMIN_RESET`` on the subscription service).

    Args:
        (none)

    Returns:
        ``{"status": "ok"}`` — the sweep is fire-and-forget; per-id
        outcomes land in audit events (``subscription.renewed`` /
        ``subscription.renewal_skipped`` / ``subscription.renew_failed``).

    Raises:
        ClientError: 403 when ``BSS_ALLOW_ADMIN_RESET`` is not set on
            the subscription service (the endpoint is disabled in
            production by design).
    """
    return await get_clients().subscription.tick_renewals_now()


@register("subscription.schedule_plan_change")
async def subscription_schedule_plan_change(
    subscription_id: SubscriptionId,
    new_offering_id: ProductOfferingId,
) -> dict[str, Any]:
    """Schedule a plan change to take effect at the next renewal boundary.

    The new offering's price is snapshotted *now* (the customer commits to
    that price even if the catalog re-prices later). At the next renewal,
    the new amount is charged, the offering is swapped, and the bundle is
    reset per the new plan's allowances. There is no proration and no
    immediate effect — that's the doctrine.

    Args:
        subscription_id: Subscription ID with the SUB- prefix (opaque suffix).
        new_offering_id: Target offering (e.g. ``PLAN_L``). Must be in the
            active catalog and different from the current plan.

    Returns:
        Updated subscription dict with ``pendingOfferingId`` /
        ``pendingOfferingPriceId`` / ``pendingEffectiveAt`` populated.

    Raises:
        PolicyViolationFromServer:
            - ``subscription.plan_change.not_eligible_state``: only active
              subscriptions can schedule changes.
            - ``subscription.plan_change.same_offering``: target equals current.
            - ``subscription.plan_change.target_not_sellable_now``: target
              is not in the live catalog right now.
            - ``subscription.plan_change.already_pending``: cancel the
              existing pending change first.
    """
    return await get_clients().subscription.schedule_plan_change(
        subscription_id, new_offering_id
    )


@register("subscription.migrate_to_new_price")
async def subscription_migrate_to_new_price(
    offering_id: ProductOfferingId,
    new_price_id: ProductOfferingPriceId,
    effective_from: IsoDatetime,
    notice_days: int = 30,
    initiated_by: str = "ops",
) -> dict[str, Any]:
    """Operator-initiated price migration with notice. Admin-only.

    Schedules ``new_price_id`` against every active subscription on
    ``offering_id`` such that ``effective_from + notice_days`` becomes the
    effective moment. The renewal flow applies the change atomically per
    subscription. Subscriptions that terminate during the notice window
    skip the migration without manual cleanup.

    Args:
        offering_id: The offering to retag (e.g. ``PLAN_M``).
        new_price_id: New ``product_offering_price.id`` belonging to that
            same offering (validated server-side).
        effective_from: ISO-8601 instant — earliest moment the new price
            may be applied. ``effective_from + notice_days`` is the actual
            apply boundary.
        notice_days: Regulatory notice (Singapore: 30 for upward moves).
        initiated_by: Operator identity stamped into the audit trail.

    Returns:
        ``{count, subscriptionIds}`` — the affected subscriptions.

    Raises:
        PolicyViolationFromServer:
            - ``subscription.admin_only``: caller lacks the admin role.
            - ``subscription.migrate_price.unknown_price``: no such price row.
            - ``subscription.migrate_price.price_not_on_offering``: target
              price belongs to a different offering than the filter.
    """
    from datetime import datetime

    return await get_clients().subscription.migrate_to_new_price(
        offering_id=offering_id,
        new_price_id=new_price_id,
        effective_from=datetime.fromisoformat(effective_from),
        notice_days=notice_days,
        initiated_by=initiated_by,
    )


@register("subscription.cancel_pending_plan_change")
async def subscription_cancel_pending_plan_change(
    subscription_id: SubscriptionId,
) -> dict[str, Any]:
    """Cancel a pending plan change. Idempotent — no-op if nothing is pending.

    Args:
        subscription_id: Subscription ID with the SUB- prefix (opaque suffix).

    Returns:
        Updated subscription dict with pending fields cleared.

    Raises:
        NotFound: unknown subscription.
    """
    return await get_clients().subscription.cancel_plan_change(subscription_id)


@register("subscription.get_esim_activation")
async def subscription_get_esim_activation(
    subscription_id: SubscriptionId,
) -> dict[str, Any]:
    """Return the LPA activation-code bundle for a subscription's eSIM. Use
    this for first-time QR display after activation, or when the customer
    needs to re-install the eSIM profile.

    Args:
        subscription_id: Subscription ID with the SUB- prefix (opaque suffix).

    Returns:
        ``{subscriptionId, iccid, imsi, activationCode, msisdn}``. The
        ``activationCode`` is the LPA string (``LPA:1$...``).

    Raises:
        NotFound: no eSIM attached to this subscription.
    """
    return await get_clients().subscription.get_esim_activation(subscription_id)
