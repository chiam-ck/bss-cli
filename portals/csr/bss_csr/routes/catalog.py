"""Catalog screens — plans, VAS, promotions (v1.6 cockpit CRM).

Strictly read-only. Catalog writes (add offering, price changes,
validity windows, promotion lifecycle) stay on the conversational
surface / ``bss admin`` CLI where the operator narrates intent and the
policy layer arbitrates — the page offers "Ask the agent" drafts for
the common ones instead of forms.
"""

from __future__ import annotations

from typing import Any

import structlog
from bss_clients.errors import ClientError
from bss_orchestrator.clients import get_clients
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..templating import templates
from ..views import field, fmt_dt, offering_allowance, offering_price

log = structlog.get_logger(__name__)
router = APIRouter()


def _plan_view(o: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": o.get("id", "?"),
        "name": o.get("name", ""),
        "price": offering_price(o),
        "lifecycle": field(o, "lifecycle_status", default="active"),
        "sellable": bool(o.get("isSellable", True)),
        "data": offering_allowance(o, "data"),
        "voice": offering_allowance(o, "voice"),
        "sms": offering_allowance(o, "sms"),
        "roaming": offering_allowance(o, "data_roaming"),
    }


@router.get("/catalog", response_class=HTMLResponse)
async def catalog_index(request: Request) -> HTMLResponse:
    clients = get_clients()
    try:
        offerings = await clients.catalog.list_offerings() or []
    except ClientError as exc:
        log.warning("csr.catalog.list_failed", status=exc.status_code)
        offerings = []
    try:
        vas = await clients.catalog.list_vas() or []
    except (ClientError, AttributeError):
        vas = []
    try:
        promotions = await clients.catalog.list_promotions() or []
    except (ClientError, AttributeError):
        promotions = []

    plans = [_plan_view(o) for o in offerings if o.get("isBundle", True)]

    vas_views = [
        {
            "id": v.get("id", "?"),
            "name": v.get("name", ""),
            "price": f"{v.get('currency', 'SGD')} {v.get('priceAmount', '?')}",
            "allowance": f"{v.get('allowanceQuantity', '—')} {v.get('allowanceUnit', '')}".strip(),
            "expiry": f"{v['expiryHours']}h" if v.get("expiryHours") else "—",
        }
        for v in vas
    ]

    promo_views = [
        {
            "id": p.get("id", "?"),
            "name": field(p, "display_name", "name", default=""),
            "code": field(p, "code", default="—"),
            "state": field(p, "state", default="?"),
            "discount": (
                f"{field(p, 'discount_type', default='')} "
                f"{field(p, 'discount_value', default='')}"
            ).strip() or "—",
            "audience": field(p, "audience", default=""),
            "valid_to": fmt_dt(field(p, "valid_to", default="")),
        }
        for p in promotions
    ]

    return templates.TemplateResponse(
        request,
        "catalog_index.html",
        {
            "active_page": "catalog",
            "model": "(env default)",
            "plans": plans,
            "vas": vas_views,
            "promotions": promo_views,
        },
    )


@router.get("/catalog/{offering_id}", response_class=HTMLResponse)
async def offering_detail(request: Request, offering_id: str) -> HTMLResponse:
    clients = get_clients()
    try:
        offering = await clients.catalog.get_offering(offering_id)
    except ClientError as exc:
        if exc.status_code == 404:
            raise HTTPException(404, f"Offering {offering_id} not found")
        raise

    try:
        active_price = await clients.catalog.get_active_price(offering_id)
    except (ClientError, AttributeError):
        active_price = None

    prices = [
        {
            "id": p.get("id", ""),
            "value": f"{((p.get('price') or {}).get('taxIncludedAmount') or {}).get('unit', 'SGD')} "
                     f"{((p.get('price') or {}).get('taxIncludedAmount') or {}).get('value', '?')}",
            "valid_from": fmt_dt(field(p, "valid_from", default="")),
            "valid_to": fmt_dt(field(p, "valid_to", default="")),
        }
        for p in offering.get("productOfferingPrice") or []
    ]

    return templates.TemplateResponse(
        request,
        "offering_detail.html",
        {
            "active_page": "catalog",
            "model": "(env default)",
            "offering": {
                **_plan_view(offering),
                "description": offering.get("description", ""),
                "valid_from": fmt_dt(field(offering, "valid_from", default="")),
                "valid_to": fmt_dt(field(offering, "valid_to", default="")),
                "is_bundle": bool(offering.get("isBundle", True)),
            },
            "prices": prices,
            "active_price_id": (active_price or {}).get("id", ""),
        },
    )
