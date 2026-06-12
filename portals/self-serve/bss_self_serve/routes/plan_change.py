"""``/plan/change`` — scheduled plan change at next renewal (v0.10 PR 10).

V0_10_0.md Track 9. Consumes v0.7's ``subscription.schedule_plan_change``
machinery: the change is staged on the subscription's
``pending_offering_id`` field and applies at the next renewal
boundary. Doctrine says no proration — that's enforced by v0.7
on the back end and stated explicitly to the customer on the
confirmation page.

Routes:

* GET ``/plan/change?subscription=SUB-X`` — ownership-checked.
  Lists v0.7's currently-sellable offerings via
  ``catalog.list_active_offerings(now)``. The customer's current
  plan is rendered with a "Current plan" badge and disabled
  CTA. If the subscription already has a pending switch, the
  pending banner + Cancel-pending-switch form sits at the top.
* POST ``/plan/change?subscription=SUB-X`` — body: ``new_offering_id``.
  ``requires_step_up('plan_change_schedule')``. One direct call
  to ``subscription.schedule_plan_change``. Same-offering / not-
  sellable / target-not-found policy violations re-render the
  list with a structured error.
* GET ``/plan/change/scheduled?subscription=SUB-X&new_offering=...&effective_at=...``
  — confirmation page. Doctrine: explicitly says "No proration."
* POST ``/plan/change/cancel`` — body: ``subscription_id``.
  ``requires_step_up('plan_change_cancel')``. One direct call to
  ``subscription.cancel_plan_change``. 303 back to /.
"""

from __future__ import annotations

from typing import Any

import structlog
from bss_clients import PolicyViolationFromServer
from bss_portal_auth import IdentityView, record_portal_action
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..clients import get_clients
from ..error_messages import is_known, render
from ..security import requires_linked_customer, requires_step_up
from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


_OWNERSHIP_RULE = "policy.ownership.subscription_not_owned"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _client_ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _identity(request: Request) -> IdentityView:
    identity: IdentityView | None = getattr(request.state, "identity", None)
    if identity is None:  # pragma: no cover
        raise RuntimeError("requires_linked_customer didn't populate identity")
    return identity


async def _check_ownership(
    clients: Any, subscription_id: str, customer_id: str
) -> dict[str, Any] | None:
    try:
        sub = await clients.subscription.get(subscription_id)
    except Exception:
        return None
    if sub.get("customerId") != customer_id:
        return None
    return sub


def _format_price(offering: dict[str, Any]) -> tuple[str, str]:
    """Pull (currency, amount) off the catalog offering response."""
    prices = offering.get("productOfferingPrice") or []
    if not prices:
        return ("SGD", "")
    p = prices[0]
    inner = (
        p.get("price", {}).get("taxIncludedAmount")
        or p.get("price", {}).get("amount")
        or {}
    )
    return (
        inner.get("unit") or inner.get("currency") or "SGD",
        str(inner.get("value") or ""),
    )


# ── GET /plan/change ─────────────────────────────────────────────────────


@router.get("/plan/change", response_class=HTMLResponse)
async def plan_change_form(
    request: Request,
    subscription: str = Query(..., min_length=1),
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    clients = get_clients()
    sub = await _check_ownership(clients, subscription, customer_id)
    if sub is None:
        return templates.TemplateResponse(
            request,
            "plan_change_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    from bss_clock import now as clock_now

    now = clock_now()
    offerings: list[dict[str, Any]] = await clients.catalog.list_active_offerings(
        at=now
    )
    current_offering_id = sub.get("offeringId")
    pending_offering_id = sub.get("pendingOfferingId")

    cards = []
    for o in offerings:
        currency, amount = _format_price(o)
        cards.append({
            "id": o.get("id"),
            "name": o.get("name") or o.get("id"),
            "currency": currency,
            "amount": amount,
            "is_current": o.get("id") == current_offering_id,
            "is_pending": o.get("id") == pending_offering_id,
            "allowances": o.get("bundleAllowance") or [],
        })

    return templates.TemplateResponse(
        request,
        "plan_change.html",
        {
            "subscription": sub,
            "current_offering_id": current_offering_id,
            "pending_offering_id": pending_offering_id,
            "pending_effective_at": sub.get("pendingEffectiveAt"),
            "next_renewal_at": sub.get("nextRenewalAt"),
            "cards": cards,
            "error": None,
        },
    )


# ── POST /plan/change — schedule ─────────────────────────────────────────


@router.post("/plan/change")
async def plan_change_submit(
    request: Request,
    subscription: str = Query(..., min_length=1),
    new_offering_id: str = Form(..., min_length=1),
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("plan_change_schedule")),
) -> Response:
    clients = get_clients()
    factory = request.app.state.db_session_factory
    iid = _identity(request).id

    sub = await _check_ownership(clients, subscription, customer_id)
    if sub is None:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="plan_change_schedule",
                route="/plan/change",
                method="POST",
                success=False,
                error_rule=_OWNERSHIP_RULE,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        return templates.TemplateResponse(
            request,
            "plan_change_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    try:
        result = await clients.subscription.schedule_plan_change(
            subscription, new_offering_id
        )
    except PolicyViolationFromServer as exc:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="plan_change_schedule",
                route="/plan/change",
                method="POST",
                success=False,
                error_rule=exc.rule,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        if not is_known(exc.rule):
            log.info("portal.plan_change.unknown_policy_rule", rule=exc.rule)
        # Re-render the form with the customer-facing error.
        from bss_clock import now as clock_now

        offerings = await clients.catalog.list_active_offerings(at=clock_now())
        cards = []
        for o in offerings:
            currency, amount = _format_price(o)
            cards.append({
                "id": o.get("id"),
                "name": o.get("name") or o.get("id"),
                "currency": currency,
                "amount": amount,
                "is_current": o.get("id") == sub.get("offeringId"),
                "is_pending": o.get("id") == sub.get("pendingOfferingId"),
                "allowances": o.get("bundleAllowance") or [],
            })
        return templates.TemplateResponse(
            request,
            "plan_change.html",
            {
                "subscription": sub,
                "current_offering_id": sub.get("offeringId"),
                "pending_offering_id": sub.get("pendingOfferingId"),
                "pending_effective_at": sub.get("pendingEffectiveAt"),
                "next_renewal_at": sub.get("nextRenewalAt"),
                "cards": cards,
                "error": render(exc.rule),
            },
            status_code=422,
        )

    async with factory() as db:
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=iid,
            action="plan_change_schedule",
            route="/plan/change",
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    effective_at = (result or {}).get("pendingEffectiveAt") or ""
    return RedirectResponse(
        url=(
            f"/plan/change/scheduled?subscription={subscription}"
            f"&new_offering={new_offering_id}"
            f"&effective_at={effective_at}"
        ),
        status_code=303,
    )


# ── POST /plan/change/cancel — cancel pending ────────────────────────────


@router.post("/plan/change/cancel")
async def plan_change_cancel(
    request: Request,
    subscription_id: str = Form(..., min_length=1),
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("plan_change_cancel")),
) -> Response:
    clients = get_clients()
    factory = request.app.state.db_session_factory
    iid = _identity(request).id

    sub = await _check_ownership(clients, subscription_id, customer_id)
    if sub is None:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="plan_change_cancel",
                route="/plan/change/cancel",
                method="POST",
                success=False,
                error_rule=_OWNERSHIP_RULE,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        return templates.TemplateResponse(
            request,
            "plan_change_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    try:
        await clients.subscription.cancel_plan_change(subscription_id)
    except PolicyViolationFromServer as exc:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="plan_change_cancel",
                route="/plan/change/cancel",
                method="POST",
                success=False,
                error_rule=exc.rule,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        return RedirectResponse(
            url=f"/?flash=plan_change_cancel_failed&rule={exc.rule}",
            status_code=303,
        )

    async with factory() as db:
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=iid,
            action="plan_change_cancel",
            route="/plan/change/cancel",
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    return RedirectResponse(url="/?flash=plan_change_cancelled", status_code=303)


# ── GET /plan/change/scheduled — confirmation page ───────────────────────


@router.get("/plan/change/scheduled", response_class=HTMLResponse)
async def plan_change_scheduled(
    request: Request,
    subscription: str = Query(..., min_length=1),
    new_offering: str = Query(..., min_length=1),
    effective_at: str = Query(default=""),
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    """Confirmation page after a successful schedule.

    The page is required to surface "No proration" explicitly per
    V0_10_0.md "Do not let plan change forget pending state" + the
    no-proration motto principle. This is the page that turns the
    abstract doctrine into a concrete sentence the customer reads
    out loud.
    """
    clients = get_clients()
    sub = await _check_ownership(clients, subscription, customer_id)
    if sub is None:
        return templates.TemplateResponse(
            request,
            "plan_change_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    # Resolve offering name for the confirmation copy.
    offerings = await clients.catalog.list_offerings()
    new_name = next(
        (o.get("name") for o in offerings if o.get("id") == new_offering),
        new_offering,
    )

    return templates.TemplateResponse(
        request,
        "plan_change_scheduled.html",
        {
            "subscription": sub,
            "new_offering_id": new_offering,
            "new_offering_name": new_name,
            "effective_at": effective_at[:10] if effective_at else None,
        },
    )
