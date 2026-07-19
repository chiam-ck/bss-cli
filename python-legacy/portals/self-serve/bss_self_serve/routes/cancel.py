"""``/subscription/<id>/cancel`` — terminate a line (v0.10 PR 7).

V0_10_0.md Track 6:

* GET ``/subscription/<id>/cancel`` — confirmation page that
  enumerates exactly what's lost (no proration / refund, the
  remaining bundle balance is discarded, the eSIM profile is
  released back to inventory, the MSISDN is released and is NOT
  retainable). Ownership-checked.
* POST ``/subscription/<id>/cancel`` —
  step-up=``subscription_terminate``; one direct call to
  ``subscription_client.terminate(reason="customer_requested")``.
* GET ``/subscription/<id>/cancelled`` — confirmation card after
  a successful terminate. Ownership rechecked.

Doctrine reminders:

* ``customer_id`` from ``request.state.customer_id``;
  ``subscription_id`` from the URL path is checked for ownership
  before any read or write. Cross-customer attempts return 403,
  not 404.
* One BSS write per route (``subscription.terminate``). The
  service-side terminate flow already handles inventory release
  + state transition + event emission; this route just calls it.
* Step-up is mandatory. Cancellation is the most destructive
  thing a customer can do to themselves on the portal — no
  bypass.
"""

from __future__ import annotations

from typing import Any

import structlog
from bss_clients import PolicyViolationFromServer
from bss_portal_auth import IdentityView, record_portal_action
from fastapi import APIRouter, Depends, Request
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


def _identity_id(request: Request) -> str | None:
    identity: IdentityView | None = getattr(request.state, "identity", None)
    return identity.id if identity is not None else None


async def _check_ownership(
    clients: Any, subscription_id: str, customer_id: str
) -> dict[str, Any] | None:
    """Server-side ownership: subscription must belong to ``customer_id``.

    Same forensic posture as PR 4 / PR 5: not-found is treated the
    same as not-yours so the response doesn't leak whether SUB-9999
    exists in the database.
    """
    try:
        sub = await clients.subscription.get(subscription_id)
    except Exception:
        return None
    if sub.get("customerId") != customer_id:
        return None
    return sub


def _losses_for(sub: dict[str, Any], balances: list[dict[str, Any]]) -> dict[str, Any]:
    """Compose the "what's lost" panel for the confirmation page.

    Doctrine: the customer must read this panel before clicking the
    submit button — no proration, no refund, balance discarded,
    inventory released. Surfacing each piece in a structured panel
    (not a paragraph) reduces the chance the customer misses one.
    """
    return {
        "current_period_end": sub.get("currentPeriodEnd"),
        "msisdn": sub.get("msisdn"),
        "iccid_last4": (sub.get("iccid") or "")[-4:] or "----",
        "balances": [
            {
                "label": (b.get("allowanceType") or "?").capitalize(),
                "remaining": b.get("remaining", 0),
                "total": b.get("total", 0),
                "unit": b.get("unit") or "",
                "unlimited": (b.get("total") or 0) < 0,
            }
            for b in balances
        ],
    }


# ── GET /subscription/<id>/cancel — confirmation page ───────────────────


@router.get("/subscription/{subscription_id}/cancel", response_class=HTMLResponse)
async def cancel_confirm(
    request: Request,
    subscription_id: str,
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    clients = get_clients()
    sub = await _check_ownership(clients, subscription_id, customer_id)
    if sub is None:
        return templates.TemplateResponse(
            request,
            "cancel_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    if sub.get("state") == "terminated":
        return templates.TemplateResponse(
            request,
            "cancel_already_terminated.html",
            {"subscription": sub},
        )

    balances: list[dict[str, Any]] = []
    try:
        balances = await clients.subscription.get_balance(subscription_id)
    except Exception:  # pragma: no cover - upstream lookup failure
        balances = []

    return templates.TemplateResponse(
        request,
        "cancel_confirm.html",
        {
            "subscription": sub,
            "losses": _losses_for(sub, balances),
            "error": None,
        },
    )


# ── POST /subscription/<id>/cancel — terminate ──────────────────────────


@router.post("/subscription/{subscription_id}/cancel")
async def cancel_submit(
    request: Request,
    subscription_id: str,
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("subscription_terminate")),
) -> Response:
    """One direct call to ``subscription.terminate``.

    The service-side flow handles inventory release (MSISDN +
    eSIM), state transition, and the ``subscription.terminated``
    event in a single transaction. This route is the customer-facing
    trigger; the BSS side is the source of truth.
    """
    clients = get_clients()
    factory = request.app.state.db_session_factory
    iid = _identity_id(request)

    sub = await _check_ownership(clients, subscription_id, customer_id)
    if sub is None:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="subscription_terminate",
                route=f"/subscription/{subscription_id}/cancel",
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
            "cancel_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    try:
        await clients.subscription.terminate(
            subscription_id, reason="customer_requested"
        )
    except PolicyViolationFromServer as exc:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="subscription_terminate",
                route=f"/subscription/{subscription_id}/cancel",
                method="POST",
                success=False,
                error_rule=exc.rule,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        if not is_known(exc.rule):
            log.info("portal.cancel.unknown_policy_rule", rule=exc.rule)
        balances: list[dict[str, Any]] = []
        try:
            balances = await clients.subscription.get_balance(subscription_id)
        except Exception:  # pragma: no cover
            balances = []
        return templates.TemplateResponse(
            request,
            "cancel_confirm.html",
            {
                "subscription": sub,
                "losses": _losses_for(sub, balances),
                "error": render(exc.rule),
            },
            status_code=422,
        )

    async with factory() as db:
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=iid,
            action="subscription_terminate",
            route=f"/subscription/{subscription_id}/cancel",
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    return RedirectResponse(
        url=f"/subscription/{subscription_id}/cancelled", status_code=303
    )


# ── GET /subscription/<id>/cancelled — success page ────────────────────


@router.get(
    "/subscription/{subscription_id}/cancelled", response_class=HTMLResponse
)
async def cancel_success(
    request: Request,
    subscription_id: str,
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    """Confirmation card after a successful terminate.

    Re-fetches the subscription so the customer sees the ``terminated``
    state directly from the BSS, not from a cached message. Ownership
    is rechecked here too — the URL is share-able and could be probed
    cross-customer.
    """
    clients = get_clients()
    sub = await _check_ownership(clients, subscription_id, customer_id)
    if sub is None:
        return templates.TemplateResponse(
            request,
            "cancel_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )
    return templates.TemplateResponse(
        request,
        "cancel_success.html",
        {"subscription": sub},
    )
