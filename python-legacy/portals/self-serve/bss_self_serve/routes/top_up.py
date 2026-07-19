"""``/top-up`` — VAS purchase from the dashboard's "Top up" CTA (v0.10).

V0_10_0.md Track 3:

* GET ``/top-up?subscription=SUB-X`` — list VAS offerings, pre-select
  if ``?context=blocked`` is set. Server-side ownership: the
  subscription must belong to ``request.state.customer_id``.
* POST ``/top-up?subscription=SUB-X`` — body: ``vas_offering_id``.
  ``requires_step_up('vas_purchase')`` gates the route. The grant
  arrives via the bss_portal_step_up cookie (set by /auth/step-up
  on the previous bounce). Direct call to
  ``subscription.purchase_vas`` — no orchestrator.

Doctrine reminders:

* ``customer_id`` is bound from ``request.state.customer_id``; the
  subscription_id from the query string is checked for ownership
  before any read or write.
* One write per route. ``purchase_vas`` is the single bss-clients
  call; the audit row is bookkeeping (separate DB).
* The step-up bounce preserves ``?subscription=`` in the URL so the
  picker page renders the right list after the OTP verify returns.
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
from ..security import (
    requires_linked_customer,
    requires_step_up,
)
from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


_OWNERSHIP_RULE = "policy.ownership.subscription_not_owned"


async def _check_ownership(
    clients: Any, subscription_id: str, customer_id: str
) -> dict[str, Any] | None:
    """Server-side ownership: the subscription must belong to ``customer_id``.

    Returns the subscription dict on success, None on mismatch /
    not-found. Callers render a 403 page on None and write a
    portal_action row with ``error_rule = policy.ownership.*``.
    """
    try:
        sub = await clients.subscription.get(subscription_id)
    except Exception:
        # Treat any not-found / lookup failure as ownership failure.
        # The customer-facing distinction between "doesn't exist" and
        # "not yours" is intentional cover for forensic purposes —
        # we don't tell a probe whether SUB-9999 exists.
        return None
    if sub.get("customerId") != customer_id:
        return None
    return sub


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _client_ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


# ── GET /top-up ──────────────────────────────────────────────────────────


@router.get("/top-up", response_class=HTMLResponse)
async def top_up_form(
    request: Request,
    subscription: str = Query(..., min_length=1),
    context: str | None = Query(default=None),
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    """List VAS offerings for the picker.

    The ``context=blocked`` query string flips the page's lead copy
    ("Your line is blocked — pick a top-up to restore service") and
    pre-selects the first ``allowance_type=data`` offering. The
    pre-selection is informational only; the customer still has to
    click submit to commit.
    """
    clients = get_clients()
    sub = await _check_ownership(clients, subscription, customer_id)
    if sub is None:
        return templates.TemplateResponse(
            request,
            "top_up_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    vas_offerings: list[dict[str, Any]] = await clients.catalog.list_vas()

    # Pre-select the first 'data' offering when the customer arrived
    # via the dashboard's "Top up to unblock" CTA.
    pre_select_id: str | None = None
    if context == "blocked":
        first_data = next(
            (v for v in vas_offerings if v.get("allowanceType") == "data"),
            None,
        )
        if first_data:
            pre_select_id = first_data["id"]

    return templates.TemplateResponse(
        request,
        "top_up.html",
        {
            "subscription": sub,
            "vas_offerings": vas_offerings,
            "pre_select_id": pre_select_id,
            "context": context,
            "error": None,
        },
    )


# ── POST /top-up ─────────────────────────────────────────────────────────


@router.post("/top-up")
async def top_up_submit(
    request: Request,
    subscription: str = Query(..., min_length=1),
    vas_offering_id: str = Form(..., min_length=1),
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("vas_purchase")),
) -> Response:
    """Direct VAS purchase via bss-clients.

    ``requires_step_up`` consumes the bss_portal_step_up grant
    cookie before this body runs; on no grant it raises
    ``StepUpRequired`` and the user bounces to ``/auth/step-up``.
    The next-URL preserves ``?subscription=`` so the picker page
    renders the right list on return.
    """
    clients = get_clients()
    factory = request.app.state.db_session_factory
    identity: IdentityView | None = getattr(request.state, "identity", None)
    identity_id = identity.id if identity is not None else None

    sub = await _check_ownership(clients, subscription, customer_id)
    if sub is None:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=identity_id,
                action="vas_purchase",
                route="/top-up",
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
            "top_up_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    try:
        await clients.subscription.purchase_vas(subscription, vas_offering_id)
    except PolicyViolationFromServer as exc:
        # Audit the failure with the structured rule code, then
        # re-render the picker with the customer-facing message.
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=identity_id,
                action="vas_purchase",
                route="/top-up",
                method="POST",
                success=False,
                error_rule=exc.rule,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()

        if not is_known(exc.rule):
            log.info(
                "portal.top_up.unknown_policy_rule",
                rule=exc.rule,
                customer_id=customer_id,
                subscription_id=subscription,
            )

        vas_offerings = await clients.catalog.list_vas()
        return templates.TemplateResponse(
            request,
            "top_up.html",
            {
                "subscription": sub,
                "vas_offerings": vas_offerings,
                "pre_select_id": vas_offering_id,
                "context": None,
                "error": render(exc.rule),
            },
            status_code=422,
        )

    # Success — audit, then redirect to the success page.
    async with factory() as db:
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=identity_id,
            action="vas_purchase",
            route="/top-up",
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    return RedirectResponse(
        url=f"/top-up/success?subscription={subscription}&vas={vas_offering_id}",
        status_code=303,
    )


# ── GET /top-up/success ──────────────────────────────────────────────────


@router.get("/top-up/success", response_class=HTMLResponse)
async def top_up_success(
    request: Request,
    subscription: str = Query(..., min_length=1),
    vas: str = Query(..., min_length=1),
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    """Confirmation page after a successful VAS purchase.

    Re-fetches the subscription and balances so the customer sees
    the fresh balance bars (not stale counters from the form page).
    Server-side ownership is rechecked because this URL is
    customer-shareable and could be probed cross-account.
    """
    clients = get_clients()
    sub = await _check_ownership(clients, subscription, customer_id)
    if sub is None:
        return templates.TemplateResponse(
            request,
            "top_up_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    balances: list[dict[str, Any]] = []
    try:
        balances = await clients.subscription.get_balance(subscription)
    except Exception:  # pragma: no cover - upstream error path
        balances = []

    # Try to resolve the VAS offering name; fall back to the id.
    vas_offerings = await clients.catalog.list_vas()
    vas_offering = next((v for v in vas_offerings if v.get("id") == vas), None)

    return templates.TemplateResponse(
        request,
        "top_up_success.html",
        {
            "subscription": sub,
            "vas_offering": vas_offering or {"id": vas, "name": vas},
            "balances": balances,
        },
    )
