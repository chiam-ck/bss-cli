"""``/payment-methods`` — card-on-file management (v0.10 PR 5).

V0_10_0.md Track 4:

* GET ``/payment-methods`` — list customer's methods, last-4 + brand,
  default toggle, "Add new" CTA.
* GET ``/payment-methods/add`` — Stripe-shaped form: card number,
  exp month/year, CVV, holder name, postal code. CVV + postal are
  ignored by the v0.10 mock provider; v1.0 (Stripe Elements) wires
  them through, the UX shape doesn't change.
* POST ``/payment-methods/add`` — step-up=``payment_method_add``;
  tokenize client-side (same sandbox pattern the orchestrator uses)
  + one ``payment.create_payment_method`` call.
* POST ``/payment-methods/<pm_id>/remove`` — step-up=``payment_method_remove``;
  ownership check; one ``payment.remove_method`` call. The
  service-side policy refuses to remove the only method while the
  customer has active lines.
* POST ``/payment-methods/<pm_id>/set-default`` —
  step-up=``payment_method_set_default``; ownership check; one
  ``payment.set_default_method`` call.

Doctrine:

* ``customer_id`` from ``request.state.customer_id``; ``pm_id`` from
  the URL path is checked for ownership against the customer's
  method list before any write.
* One BSS write per route. Tokenization is sandbox-side (no BSS
  write); the bss-clients call is the single write.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


def _add_template(payment_provider: str) -> str:
    """v0.16 — pick the add-card template by mode.

    Stripe-mode template is Elements-only (PCI doctrine: NO PAN bytes
    in the production-deployed template). Mock-mode template stays as
    the v0.10 server-rendered card-number form.
    """
    return (
        "payment_methods_add.html"
        if payment_provider == "stripe"
        else "payment_methods_add_mock.html"
    )

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


_OWNERSHIP_RULE = "policy.ownership.payment_method_not_owned"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _client_ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _identity_id(request: Request) -> str | None:
    identity: IdentityView | None = getattr(request.state, "identity", None)
    return identity.id if identity is not None else None


def _local_tokenize(card_number: str) -> dict[str, str]:
    """Client-side sandbox tokenizer.

    Mirrors the orchestrator's ``_local_tokenize`` so the v1.0 cutover
    to real Stripe.js / Adyen tokenization doesn't change the wire
    shape from the portal to the payment service. Embeds ``FAIL`` /
    ``DECLINE`` in the token so payment-side mock can simulate
    declines deterministically.
    """
    digits = card_number.replace(" ", "").replace("-", "")
    if not digits.isdigit() or len(digits) < 12:
        raise ValueError("Card number is invalid.")
    last4 = digits[-4:]
    bin2 = digits[:2]
    if digits[0] == "4":
        brand = "visa"
    elif 51 <= int(bin2) <= 55:
        brand = "mastercard"
    elif bin2 in ("34", "37"):
        brand = "amex"
    else:
        brand = "unknown"
    uid = str(uuid4())
    upper = card_number.upper()
    if "FAIL" in upper:
        token = f"tok_FAIL_{uid}"
    elif "DECLINE" in upper:
        token = f"tok_DECLINE_{uid}"
    else:
        token = f"tok_{uid}"
    return {"cardToken": token, "last4": last4, "brand": brand}


async def _list_owned(
    clients: Any, customer_id: str
) -> list[dict[str, Any]]:
    return await clients.payment.list_methods(customer_id)


async def _check_method_ownership(
    clients: Any, pm_id: str, customer_id: str
) -> dict[str, Any] | None:
    """Return the method dict iff it belongs to ``customer_id`` and is active.

    Same forensic posture as ``check_subscription_owned_by``: the
    customer-facing distinction between "doesn't exist" and "not
    yours" is intentional cover.
    """
    methods = await _list_owned(clients, customer_id)
    for m in methods:
        if m.get("id") == pm_id:
            return m
    return None


# ── GET /payment-methods ─────────────────────────────────────────────────


@router.get("/payment-methods", response_class=HTMLResponse)
async def list_methods(
    request: Request,
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    methods = await _list_owned(get_clients(), customer_id)
    return templates.TemplateResponse(
        request,
        "payment_methods.html",
        {"methods": methods, "error": None, "flash": None},
    )


# ── GET /payment-methods/add ─────────────────────────────────────────────


@router.get("/payment-methods/add", response_class=HTMLResponse)
async def add_method_form(
    request: Request,
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    payment_provider = getattr(request.app.state, "payment_provider", "mock")
    return templates.TemplateResponse(
        request,
        _add_template(payment_provider),
        {
            "error": None,
            "fields": {},
            "payment_provider": payment_provider,
        },
    )


# ── POST /payment-methods/add (mock-mode card-form submit) ──────────────
#
# v0.16 Track 2 redo — this route now ONLY handles the mock-mode form
# submission. Stripe-mode add-card goes through a separate two-leg
# flow: POST /payment-methods/add/checkout-init → 303 to Stripe →
# GET /payment-methods/add/checkout-return → register pm_*.


@router.post("/payment-methods/add")
async def add_method(
    request: Request,
    card_number: str = Form(default=""),
    exp_month: int | None = Form(default=None),
    exp_year: int | None = Form(default=None),
    cvv: str = Form(default=""),  # noqa: ARG001 — Stripe-shaped seam
    holder_name: str = Form(default=""),  # noqa: ARG001 — surfaced in v1.0
    postal_code: str | None = Form(default=None),  # noqa: ARG001 — surfaced in v1.0
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("payment_method_add")),
) -> Response:
    """Add a card on file (mock-mode only).

    Stripe-mode is served by /payment-methods/add/checkout-init →
    Stripe Checkout → /payment-methods/add/checkout-return.
    """
    clients = get_clients()
    factory = request.app.state.db_session_factory
    iid = _identity_id(request)
    payment_provider = getattr(request.app.state, "payment_provider", "mock")

    if payment_provider == "stripe":
        # Wrong route for stripe-mode — bounce to the Checkout-init
        # entry point. Browsers that submit the bare form here (no
        # mock fields) land here cleanly.
        return RedirectResponse(
            url="/payment-methods/add/checkout-init", status_code=303
        )

    # Mock path — preserves the v0.10 server-side tokenize behavior.
    if (
        not card_number
        or len(card_number) < 12
        or exp_month is None
        or not (1 <= exp_month <= 12)
        or exp_year is None
        or not (2026 <= exp_year <= 2099)
        or not cvv
        or len(cvv) < 3
        or len(cvv) > 4
        or not holder_name
    ):
        return templates.TemplateResponse(
            request,
            _add_template(payment_provider),
            {
                "error": "Please fill in every field with a valid value.",
                "fields": {"exp_month": exp_month, "exp_year": exp_year},
                "payment_provider": payment_provider,
            },
            status_code=422,
        )

    try:
        tok = _local_tokenize(card_number)
    except ValueError:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="payment_method_add",
                route="/payment-methods/add",
                method="POST",
                success=False,
                error_rule="policy.payment.method.invalid_card",
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        return templates.TemplateResponse(
            request,
            _add_template(payment_provider),
            {
                "error": "That card number doesn't look right. Check the digits.",
                "fields": {"exp_month": exp_month, "exp_year": exp_year},
                "payment_provider": payment_provider,
            },
            status_code=422,
        )

    try:
        await clients.payment.create_payment_method(
            customer_id=customer_id,
            card_token=tok["cardToken"],
            last4=tok["last4"],
            brand=tok["brand"],
            exp_month=exp_month,
            exp_year=exp_year,
        )
    except PolicyViolationFromServer as exc:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="payment_method_add",
                route="/payment-methods/add",
                method="POST",
                success=False,
                error_rule=exc.rule,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        if not is_known(exc.rule):
            log.info("portal.payment_methods.unknown_policy_rule", rule=exc.rule)
        return templates.TemplateResponse(
            request,
            _add_template(payment_provider),
            {
                "error": render(exc.rule),
                "fields": {"exp_month": exp_month, "exp_year": exp_year},
                "payment_provider": payment_provider,
            },
            status_code=422,
        )

    async with factory() as db:
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=iid,
            action="payment_method_add",
            route="/payment-methods/add",
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    return RedirectResponse(url="/payment-methods?flash=added", status_code=303)


# ── v0.16 Stripe Checkout: init + return ────────────────────────────────


@router.post("/payment-methods/add/checkout-init")
async def add_method_checkout_init(
    request: Request,
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("payment_method_add")),
) -> Response:
    """Mint a Stripe CheckoutSession + 303 redirect to Stripe.

    Same shape as the signup flow's checkout-init: ensure cus_*, create
    session in mode=setup, send the customer to Stripe's hosted form.
    """
    import asyncio

    import stripe

    settings = request.app.state.settings
    api_key = getattr(request.app.state, "payment_stripe_api_key", "")
    public_url = settings.bss_portal_public_url.rstrip("/")
    if not api_key:
        return templates.TemplateResponse(
            request,
            _add_template("stripe"),
            {
                "error": "Payment is misconfigured (Stripe key missing). "
                "Please contact support.",
                "fields": {},
                "payment_provider": "stripe",
            },
            status_code=503,
        )

    clients = get_clients()
    iid = _identity_id(request)
    factory = request.app.state.db_session_factory

    # Look up the customer's email to seed Stripe's customer record.
    # We have the customer_id via the verified-session dep; the email
    # lives on portal_auth.identity (already validated by login).
    email = ""
    if iid:
        try:
            from bss_portal_auth.models import Identity
            from sqlalchemy import select

            async with factory() as db:
                row = (
                    await db.execute(
                        select(Identity).where(Identity.id == iid)
                    )
                ).scalar_one_or_none()
                if row:
                    email = row.email
        except Exception:  # noqa: BLE001 — best-effort lookup
            email = ""

    try:
        ensure_resp = await clients.payment.ensure_customer(
            customer_id=customer_id,
            email=email or f"{customer_id}@bss-cli.local",
        )
        cus_id = ensure_resp["customer_external_ref"]
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "portal.payment_methods.ensure_customer_failed", error=str(exc)
        )
        return templates.TemplateResponse(
            request,
            _add_template("stripe"),
            {
                "error": "Couldn't reach the payment service. Please try again.",
                "fields": {},
                "payment_provider": "stripe",
            },
            status_code=503,
        )

    return_url = (
        f"{public_url}/payment-methods/add/checkout-return"
        f"?cs_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = f"{public_url}/payment-methods"
    try:
        cs = await asyncio.to_thread(
            stripe.checkout.Session.create,
            api_key=api_key,
            mode="setup",
            payment_method_types=["card"],
            customer=cus_id,
            success_url=return_url,
            cancel_url=cancel_url,
            metadata={"bss_customer_id": customer_id, "bss_action": "add_pm"},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "portal.payment_methods.checkout_session_create_failed",
            customer_id=customer_id,
            error=str(exc),
        )
        return templates.TemplateResponse(
            request,
            _add_template("stripe"),
            {
                "error": "Couldn't reach Stripe. Please try again.",
                "fields": {},
                "payment_provider": "stripe",
            },
            status_code=503,
        )

    cs_url = cs.get("url") if isinstance(cs, dict) else cs.url
    return RedirectResponse(url=cs_url, status_code=303)


@router.get("/payment-methods/add/checkout-return")
async def add_method_checkout_return(
    request: Request,
    cs_id: str = Query(...),
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    """Stripe redirects the customer back here after Checkout.

    Retrieves the session, extracts pm_*, registers via bss-clients,
    redirects to /payment-methods.
    """
    import asyncio

    import stripe

    api_key = getattr(request.app.state, "payment_stripe_api_key", "")
    if not api_key or not cs_id.startswith("cs_"):
        return RedirectResponse("/payment-methods?flash=error", status_code=303)

    try:
        cs = await asyncio.to_thread(
            stripe.checkout.Session.retrieve,
            cs_id,
            api_key=api_key,
            expand=["setup_intent"],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "portal.payment_methods.checkout_session_retrieve_failed",
            cs_id=cs_id,
            error=str(exc),
        )
        return RedirectResponse("/payment-methods?flash=error", status_code=303)

    cs_dict = cs.to_dict() if hasattr(cs, "to_dict") else dict(cs)
    meta = cs_dict.get("metadata") or {}
    if meta.get("bss_customer_id") != customer_id:
        log.warning(
            "portal.payment_methods.checkout_metadata_mismatch",
            cs_id=cs_id,
            session_customer=customer_id,
            cs_customer=meta.get("bss_customer_id"),
        )
        return RedirectResponse("/payment-methods?flash=error", status_code=303)

    si = cs_dict.get("setup_intent") or {}
    if isinstance(si, str):
        si_obj = await asyncio.to_thread(
            stripe.SetupIntent.retrieve, si, api_key=api_key
        )
        si = si_obj.to_dict() if hasattr(si_obj, "to_dict") else dict(si_obj)
    pm_id = si.get("payment_method") if isinstance(si, dict) else None
    if not pm_id or not str(pm_id).startswith("pm_"):
        return RedirectResponse("/payment-methods?flash=error", status_code=303)

    clients = get_clients()
    try:
        await clients.payment.create_payment_method(
            customer_id=customer_id,
            card_token=pm_id,
            last4="",
            brand="",
            tokenization_provider="stripe",
        )
    except PolicyViolationFromServer as exc:
        if not is_known(exc.rule):
            log.info(
                "portal.payment_methods.unknown_policy_rule", rule=exc.rule
            )
        return RedirectResponse(
            f"/payment-methods?flash=error&rule={exc.rule}",
            status_code=303,
        )

    return RedirectResponse("/payment-methods?flash=added", status_code=303)


# ── POST /payment-methods/<pm_id>/remove ─────────────────────────────────


@router.post("/payment-methods/{pm_id}/remove")
async def remove_method(
    request: Request,
    pm_id: str,
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("payment_method_remove")),
) -> Response:
    clients = get_clients()
    factory = request.app.state.db_session_factory
    iid = _identity_id(request)

    owned = await _check_method_ownership(clients, pm_id, customer_id)
    if owned is None:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="payment_method_remove",
                route=f"/payment-methods/{pm_id}/remove",
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
            "payment_methods_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    try:
        await clients.payment.remove_method(pm_id)
    except PolicyViolationFromServer as exc:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="payment_method_remove",
                route=f"/payment-methods/{pm_id}/remove",
                method="POST",
                success=False,
                error_rule=exc.rule,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        # Re-render the list with the structured error visible — the
        # canonical example here is "can't remove the only method
        # while you have an active line".
        methods = await _list_owned(clients, customer_id)
        return templates.TemplateResponse(
            request,
            "payment_methods.html",
            {"methods": methods, "error": render(exc.rule), "flash": None},
            status_code=422,
        )

    async with factory() as db:
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=iid,
            action="payment_method_remove",
            route=f"/payment-methods/{pm_id}/remove",
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    return RedirectResponse(url="/payment-methods?flash=removed", status_code=303)


# ── POST /payment-methods/<pm_id>/set-default ────────────────────────────


@router.post("/payment-methods/{pm_id}/set-default")
async def set_default(
    request: Request,
    pm_id: str,
    customer_id: str = Depends(requires_linked_customer),
    _step_up: None = Depends(requires_step_up("payment_method_set_default")),
) -> Response:
    clients = get_clients()
    factory = request.app.state.db_session_factory
    iid = _identity_id(request)

    owned = await _check_method_ownership(clients, pm_id, customer_id)
    if owned is None:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="payment_method_set_default",
                route=f"/payment-methods/{pm_id}/set-default",
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
            "payment_methods_forbidden.html",
            {"customer_facing": render(_OWNERSHIP_RULE)},
            status_code=403,
        )

    try:
        await clients.payment.set_default_method(pm_id)
    except PolicyViolationFromServer as exc:
        async with factory() as db:
            await record_portal_action(
                db,
                customer_id=customer_id,
                identity_id=iid,
                action="payment_method_set_default",
                route=f"/payment-methods/{pm_id}/set-default",
                method="POST",
                success=False,
                error_rule=exc.rule,
                step_up_consumed=True,
                ip=_client_ip(request),
                user_agent=_client_ua(request),
            )
            await db.commit()
        methods = await _list_owned(clients, customer_id)
        return templates.TemplateResponse(
            request,
            "payment_methods.html",
            {"methods": methods, "error": render(exc.rule), "flash": None},
            status_code=422,
        )

    async with factory() as db:
        await record_portal_action(
            db,
            customer_id=customer_id,
            identity_id=iid,
            action="payment_method_set_default",
            route=f"/payment-methods/{pm_id}/set-default",
            method="POST",
            success=True,
            step_up_consumed=True,
            ip=_client_ip(request),
            user_agent=_client_ua(request),
        )
        await db.commit()

    return RedirectResponse(url="/payment-methods?flash=default_set", status_code=303)
