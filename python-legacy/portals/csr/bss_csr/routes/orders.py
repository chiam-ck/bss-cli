"""Order screens — cross-customer queue + COM/SOM detail (v1.6 cockpit CRM).

The queue rides the v1.6 COM extension (``GET /productOrder`` without
``customerId``). v1.6.1 (operator directive) — full order CRUD is
direct: create from the queue page, submit/cancel from the detail page.
Submit charges the card-on-file at activation and cancel is on the
destructive list, so both sit behind the two-step UI confirm
(``confirm=yes``); the COM policy layer stays the server-side gate.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlencode

import structlog
from bss_clients.errors import ClientError, PolicyViolationFromServer
from bss_orchestrator.clients import get_clients
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..templating import templates
from ..views import field, flatten_order, fmt_dt

log = structlog.get_logger(__name__)
router = APIRouter()

PAGE_SIZE = 25

ORDER_STATES = [
    "draft", "submitted", "awaiting_payment", "in_progress",
    "completed", "cancelled", "failed",
]


@router.get("/orders", response_class=HTMLResponse)
async def orders_list(
    request: Request,
    customer: str = "",
    state: str = "",
    page: int = Query(default=0, ge=0, le=10_000),
) -> HTMLResponse:
    customer_clean = customer.strip()
    state_clean = state.strip()
    clients = get_clients()
    try:
        raw = await clients.com.list_orders(
            customer_clean or None,
            state=state_clean or None,
            limit=PAGE_SIZE + 1,
            offset=page * PAGE_SIZE,
        )
    except ClientError as exc:
        log.warning("csr.orders.list_failed", status=exc.status_code)
        raw = []
    has_next = len(raw or []) > PAGE_SIZE
    rows = [flatten_order(o) for o in (raw or [])[:PAGE_SIZE]]

    # Plan ids for the create-order select — best-effort.
    try:
        plans = [
            o.get("id", "")
            for o in await clients.catalog.list_active_offerings() or []
            if o.get("isBundle", True)
        ]
    except Exception:  # noqa: BLE001
        plans = []

    return templates.TemplateResponse(
        request,
        "orders_list.html",
        {
            "active_page": "orders",
            "model": "(env default)",
            "customer": customer_clean,
            "state": state_clean,
            "states": ORDER_STATES,
            "rows": rows,
            "plans": plans,
            "page": page,
            "has_prev": page > 0,
            "has_next": has_next,
            "flash": request.query_params.get("flash", ""),
            "err": request.query_params.get("err", "")[:300],
        },
    )


CONFIRM_REQUIRED = "This action needs the expanded confirm step."


def _back_to_order(order_id: str, **params: str) -> RedirectResponse:
    url = f"/orders/{order_id}"
    filtered = {k: v for k, v in params.items() if v}
    if filtered:
        url += "?" + urlencode(filtered)
    return RedirectResponse(url=url, status_code=303)


@router.post("/orders/create", response_model=None)
async def create_order(
    customer_id: str = Form(...),
    offering_id: str = Form(...),
    msisdn_preference: str = Form(default=""),
    discount_code: str = Form(default=""),
) -> RedirectResponse:
    try:
        order = await get_clients().com.create_order(
            customer_id=customer_id.strip(),
            offering_id=offering_id.strip(),
            msisdn_preference=msisdn_preference.strip() or None,
            discount_code=discount_code.strip() or None,
        )
    except PolicyViolationFromServer as exc:
        return RedirectResponse(
            url="/orders?" + urlencode({"err": exc.detail}), status_code=303
        )
    except ClientError as exc:
        return RedirectResponse(
            url="/orders?" + urlencode({"err": f"COM error ({exc.status_code})"}),
            status_code=303,
        )
    return _back_to_order(order.get("id", ""), flash="order_created")


@router.post("/orders/{order_id}/submit", response_model=None)
async def submit_order(
    order_id: str, confirm: str = Form(default="")
) -> RedirectResponse:
    if confirm != "yes":
        return _back_to_order(order_id, err=CONFIRM_REQUIRED)
    try:
        await get_clients().com.submit_order(order_id)
    except PolicyViolationFromServer as exc:
        return _back_to_order(order_id, err=exc.detail)
    except ClientError as exc:
        return _back_to_order(order_id, err=f"COM error ({exc.status_code})")
    return _back_to_order(order_id, flash="order_submitted")


@router.post("/orders/{order_id}/cancel", response_model=None)
async def cancel_order(
    order_id: str, confirm: str = Form(default="")
) -> RedirectResponse:
    if confirm != "yes":
        return _back_to_order(order_id, err=CONFIRM_REQUIRED)
    try:
        await get_clients().com.cancel_order(order_id)
    except PolicyViolationFromServer as exc:
        return _back_to_order(order_id, err=exc.detail)
    except ClientError as exc:
        return _back_to_order(order_id, err=f"COM error ({exc.status_code})")
    return _back_to_order(order_id, flash="order_cancelled")


@router.get("/orders/jump", response_model=None)
async def orders_jump(order_id: str = "") -> RedirectResponse:
    target = order_id.strip()
    if not target:
        return RedirectResponse(url="/orders", status_code=303)
    return RedirectResponse(url=f"/orders/{target}", status_code=303)


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, order_id: str) -> HTMLResponse:
    clients = get_clients()
    try:
        order = await clients.com.get_order(order_id)
    except ClientError as exc:
        if exc.status_code == 404:
            raise HTTPException(404, f"Order {order_id} not found")
        raise

    # SOM decomposition — best-effort; COM is the page's source of truth.
    service_orders: list[dict[str, Any]] = []
    try:
        service_orders = await clients.som.list_for_order(order_id) or []
    except Exception as exc:  # noqa: BLE001 — SOM down ≠ order page down
        log.warning("csr.orders.som_fetch_failed", order_id=order_id, error=str(exc))

    async def _services_for(so: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in so.get("items") or []:
            svc_id = field(item, "target_service_id", default=None)
            if not svc_id:
                continue
            try:
                out.append(await clients.som.get_service(svc_id))
            except Exception:  # noqa: BLE001
                continue
        return out

    services_per_so = await asyncio.gather(
        *(_services_for(so) for so in service_orders)
    ) if service_orders else []

    so_views = []
    for so, services in zip(service_orders, services_per_so):
        so_views.append(
            {
                "id": so.get("id", "?"),
                "state": field(so, "state", default="?"),
                "started_at": fmt_dt(field(so, "started_at", default="")),
                "completed_at": fmt_dt(field(so, "completed_at", default="")),
                # "so_items", not "items" — Jinja resolves attributes
                # before subscripts, and dict.items (the method) wins.
                "so_items": [
                    {
                        "action": field(i, "action", default="—"),
                        "spec_id": field(i, "service_spec_id", default="—"),
                        "service_id": field(i, "target_service_id", default=""),
                    }
                    for i in so.get("items") or []
                ],
                "services": [
                    {
                        "id": s.get("id", "?"),
                        "type": field(s, "type", "service_type", default="—"),
                        "spec_id": field(s, "spec_id", default=""),
                        "state": field(s, "state", default="?"),
                    }
                    for s in services
                ],
            }
        )

    items = [
        {
            "id": i.get("id", ""),
            "offering_id": field(i, "offering_id", default="—"),
            "action": field(i, "action", default=""),
            "state": field(i, "state", default=""),
            "price": field(i, "price_amount", "price", default=""),
            "msisdn": field(i, "msisdn", default=""),
        }
        for i in order.get("items") or []
    ]

    return templates.TemplateResponse(
        request,
        "order_detail.html",
        {
            "active_page": "orders",
            "model": "(env default)",
            "order": {
                **flatten_order(order),
                "notes": field(order, "notes", default=""),
                "subscription_id": field(order, "subscription_id", default=""),
                "discount_code": field(order, "discount_code", default=""),
            },
            "items": items,
            "service_orders": so_views,
            "flash": request.query_params.get("flash", ""),
            "err": request.query_params.get("err", "")[:300],
        },
    )
