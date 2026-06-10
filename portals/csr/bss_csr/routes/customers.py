"""Customer screens — list/search + 360 detail (v1.6 cockpit CRM).

Reads go direct via ``bss_orchestrator.clients.get_clients`` (same as
the search and case routes since v0.13). Writes are single policy-gated
``bss-clients`` calls: interactions, cases, name + contact-medium CRUD,
and (v1.6.1, operator directive) ``customer.close`` /
``remove_contact_medium`` as direct CRUD behind the two-step UI confirm
(``confirm=yes`` from the expanded danger panel; routes refuse without
it). "Ask the agent" handoffs remain for narrative/compound work.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlencode

import structlog
from bss_clients.errors import ClientError, PolicyViolationFromServer
from bss_orchestrator.clients import get_clients
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..templating import templates
from ..views import (
    balance_rows,
    customer_name,
    field,
    flatten_case,
    flatten_customer,
    flatten_order,
    fmt_dt,
)

log = structlog.get_logger(__name__)
router = APIRouter()

PAGE_SIZE = 25
_MSISDN_RE = re.compile(r"^\+?\d{6,}$")

CUSTOMER_STATES = ["active", "suspended", "closed"]


@router.get("/customers", response_class=HTMLResponse)
async def customers_list(
    request: Request,
    q: str = "",
    state: str = "",
    page: int = Query(default=0, ge=0, le=10_000),
) -> HTMLResponse:
    q_clean = q.strip()
    state_clean = state.strip()
    clients = get_clients()
    rows: list[dict[str, Any]] = []
    has_next = False

    if q_clean and _MSISDN_RE.match(q_clean):
        digits = q_clean.lstrip("+").replace(" ", "")
        try:
            cust = await clients.crm.find_customer_by_msisdn(digits)
        except ClientError:
            cust = None
        if cust:
            rows = [flatten_customer(cust)]
    else:
        try:
            # Fetch one extra row to know whether a next page exists.
            raw = await clients.crm.list_customers(
                name_contains=q_clean or None,
                state=state_clean or None,
                limit=PAGE_SIZE + 1,
                offset=page * PAGE_SIZE,
            )
        except ClientError as exc:
            log.warning("csr.customers.list_failed", status=exc.status_code)
            raw = []
        has_next = len(raw or []) > PAGE_SIZE
        rows = [flatten_customer(c) for c in (raw or [])[:PAGE_SIZE]]

    return templates.TemplateResponse(
        request,
        "customers_list.html",
        {
            "active_page": "customers",
            "model": "(env default)",
            "q": q_clean,
            "state": state_clean,
            "states": CUSTOMER_STATES,
            "rows": rows,
            "page": page,
            "has_prev": page > 0,
            "has_next": has_next,
        },
    )


async def _gather_section(coro) -> tuple[Any, bool]:
    """Await a section fetch; (payload, ok). Sections degrade independently
    so one slow/down service doesn't blank the whole 360."""
    try:
        return await coro, True
    except Exception as exc:  # noqa: BLE001 — best-effort read fan-out
        log.warning("csr.customer_360.section_failed", error=str(exc))
        return None, False


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
async def customer_detail(
    request: Request, customer_id: str
) -> HTMLResponse:
    clients = get_clients()
    try:
        cust = await clients.crm.get_customer(customer_id)
    except ClientError as exc:
        if exc.status_code == 404:
            raise HTTPException(404, f"Customer {customer_id} not found")
        raise

    (
        (subs, subs_ok),
        (orders, orders_ok),
        (cases, cases_ok),
        (interactions, interactions_ok),
        (methods, methods_ok),
        (kyc, _),
    ) = await asyncio.gather(
        _gather_section(clients.subscription.list_for_customer(customer_id)),
        _gather_section(clients.com.list_orders(customer_id)),
        _gather_section(clients.crm.list_cases(customer_id=customer_id)),
        _gather_section(clients.crm.list_interactions(customer_id, limit=15)),
        _gather_section(clients.payment.list_methods(customer_id)),
        _gather_section(clients.crm.get_kyc_status(customer_id)),
    )

    sub_views = []
    for s in subs or []:
        sub_views.append(
            {
                "id": s.get("id", "?"),
                "offering_id": field(s, "offering_id", default="—"),
                "msisdn": s.get("msisdn", "—"),
                "state": field(s, "state", default="?"),
                "next_renewal": fmt_dt(field(s, "next_renewal_at", default="")),
                "balances": balance_rows(s.get("balances")),
            }
        )

    method_views = []
    for m in methods or []:
        card = m.get("cardSummary") or m.get("card_summary") or {}
        method_views.append(
            {
                "id": m.get("id", "?"),
                "brand": field(card, "brand", default="card"),
                "last4": field(card, "last4", "masked_pan", default="????"),
                "exp": f"{field(card, 'exp_month', default='??')}/{field(card, 'exp_year', default='??')}",
                "is_default": bool(field(m, "is_default", default=False)),
                "status": field(m, "status", default=""),
            }
        )

    interaction_views = [
        {
            "at": fmt_dt(field(i, "occurred_at", "created_at", default="")),
            "channel": field(i, "channel", default="—"),
            "direction": field(i, "direction", default=""),
            "summary": field(i, "summary", "action", default=""),
        }
        for i in (interactions or [])
    ]

    flat = flatten_customer(cust)
    individual = cust.get("individual") or {}
    contact_mediums = [
        {
            "id": cm.get("id", ""),
            "type": cm.get("mediumType", "?"),
            "value": cm.get("value", "")
            or (cm.get("characteristic") or {}).get("emailAddress", "")
            or (cm.get("characteristic") or {}).get("phoneNumber", ""),
        }
        for cm in cust.get("contactMedium") or []
    ]

    return templates.TemplateResponse(
        request,
        "customer_detail.html",
        {
            "active_page": "customers",
            "model": "(env default)",
            "customer": flat,
            "customer_raw_name": customer_name(cust),
            "given_name": individual.get("givenName", ""),
            "family_name": individual.get("familyName", ""),
            "contact_mediums": contact_mediums,
            "kyc": kyc or {},
            "subscriptions": sub_views,
            "subs_ok": subs_ok,
            "orders": [flatten_order(o) for o in (orders or [])],
            "orders_ok": orders_ok,
            "cases": [flatten_case(c) for c in (cases or [])],
            "cases_ok": cases_ok,
            "interactions": interaction_views,
            "interactions_ok": interactions_ok,
            "payment_methods": method_views,
            "methods_ok": methods_ok,
            "flash": request.query_params.get("flash", ""),
            "err": request.query_params.get("err", "")[:300],
        },
    )


def _back_to_customer(customer_id: str, **params: str) -> RedirectResponse:
    url = f"/customers/{customer_id}"
    filtered = {k: v for k, v in params.items() if v}
    if filtered:
        url += "?" + urlencode(filtered)
    return RedirectResponse(url=url, status_code=303)


@router.post("/customers/{customer_id}/interaction", response_model=None)
async def log_interaction(
    customer_id: str,
    summary: str = Form(...),
    direction: str = Form(default="inbound"),
) -> RedirectResponse:
    clients = get_clients()
    try:
        await clients.crm.log_interaction(
            customer_id=customer_id,
            summary=summary.strip(),
            channel="portal-csr",
            direction=direction if direction in ("inbound", "outbound") else "inbound",
        )
    except PolicyViolationFromServer as exc:
        return _back_to_customer(customer_id, err=exc.detail)
    except ClientError as exc:
        return _back_to_customer(customer_id, err=f"CRM error ({exc.status_code})")
    return _back_to_customer(customer_id, flash="interaction_logged")


CONFIRM_REQUIRED = "This action needs the expanded confirm step."


async def _write(customer_id: str, action: str, coro) -> RedirectResponse:
    """Run one customer write; flash the outcome back onto the 360."""
    try:
        await coro
    except PolicyViolationFromServer as exc:
        return _back_to_customer(customer_id, err=exc.detail)
    except ClientError as exc:
        return _back_to_customer(customer_id, err=f"CRM error ({exc.status_code})")
    return _back_to_customer(customer_id, flash=action)


@router.post("/customers/{customer_id}/name", response_model=None)
async def update_name(
    customer_id: str,
    given_name: str = Form(...),
    family_name: str = Form(...),
) -> RedirectResponse:
    return await _write(
        customer_id, "name_updated",
        get_clients().crm.update_individual(
            customer_id,
            given_name=given_name.strip(),
            family_name=family_name.strip(),
        ),
    )


@router.post("/customers/{customer_id}/contact", response_model=None)
async def add_contact(
    customer_id: str,
    medium_type: str = Form(...),
    value: str = Form(...),
) -> RedirectResponse:
    mtype = medium_type if medium_type in ("email", "mobile") else "email"
    return await _write(
        customer_id, "contact_added",
        get_clients().crm.add_contact_medium(
            customer_id, medium_type=mtype, value=value.strip()
        ),
    )


@router.post("/customers/{customer_id}/contact/{medium_id}", response_model=None)
async def update_contact(
    customer_id: str,
    medium_id: str,
    value: str = Form(...),
) -> RedirectResponse:
    return await _write(
        customer_id, "contact_updated",
        get_clients().crm.update_contact_medium(
            customer_id, medium_id, value=value.strip()
        ),
    )


@router.post(
    "/customers/{customer_id}/contact/{medium_id}/remove", response_model=None
)
async def remove_contact(
    customer_id: str,
    medium_id: str,
    confirm: str = Form(default=""),
) -> RedirectResponse:
    if confirm != "yes":
        return _back_to_customer(customer_id, err=CONFIRM_REQUIRED)
    return await _write(
        customer_id, "contact_removed",
        get_clients().crm.remove_contact_medium(customer_id, medium_id),
    )


@router.post("/customers/{customer_id}/close", response_model=None)
async def close_customer(
    customer_id: str,
    confirm: str = Form(default=""),
) -> RedirectResponse:
    if confirm != "yes":
        return _back_to_customer(customer_id, err=CONFIRM_REQUIRED)
    return await _write(
        customer_id, "customer_closed",
        get_clients().crm.close_customer(customer_id),
    )


@router.post("/customers/{customer_id}/case", response_model=None)
async def open_case(
    customer_id: str,
    subject: str = Form(...),
    category: str = Form(default="technical"),
    priority: str = Form(default="normal"),
    description: str = Form(default=""),
) -> RedirectResponse:
    clients = get_clients()
    try:
        case = await clients.crm.open_case(
            customer_id=customer_id,
            subject=subject.strip(),
            category=category,
            priority=priority,
            description=description.strip() or None,
        )
    except PolicyViolationFromServer as exc:
        return _back_to_customer(customer_id, err=exc.detail)
    except ClientError as exc:
        return _back_to_customer(customer_id, err=f"CRM error ({exc.status_code})")
    case_id = case.get("id", "")
    return RedirectResponse(
        url=f"/case/{case_id}?flash=case_opened", status_code=303
    )
