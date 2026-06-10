"""Catalog screens — plans, VAS, promotions (v1.6 cockpit CRM).

v1.6.1 (operator directive) — catalog admin CRUD is direct: add an
offering, add a price row, set a validity window; the same
``admin_*`` client surface the ``bss admin catalog`` CLI uses, policy-
gated server-side. Promotion lifecycle stays chat/CLI-only because
``bss promo assign`` composes loyalty pairing (v1.3) on top of the
catalog write — a bare UI form would silently skip the loyalty mint.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import structlog
from bss_clients.errors import ClientError, PolicyViolationFromServer
from bss_orchestrator.clients import get_clients
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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
            "flash": request.query_params.get("flash", ""),
            "err": request.query_params.get("err", "")[:300],
        },
    )


def _parse_dt(raw: str) -> datetime | None:
    """datetime-local input ('2026-06-10T00:00') → datetime, or None."""
    raw = raw.strip()
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def _back(url: str, **params: str) -> RedirectResponse:
    filtered = {k: v for k, v in params.items() if v}
    if filtered:
        url += "?" + urlencode(filtered)
    return RedirectResponse(url=url, status_code=303)


@router.post("/catalog/offering", response_model=None)
async def add_offering(
    offering_id: str = Form(...),
    name: str = Form(...),
    amount: str = Form(...),
    data_mb: int | None = Form(default=None),
    voice_minutes: int | None = Form(default=None),
    sms_count: int | None = Form(default=None),
    data_roaming_mb: int | None = Form(default=None),
) -> RedirectResponse:
    try:
        await get_clients().catalog.admin_add_offering(
            offering_id=offering_id.strip(),
            name=name.strip(),
            amount=amount.strip(),
            data_mb=data_mb,
            voice_minutes=voice_minutes,
            sms_count=sms_count,
            data_roaming_mb=data_roaming_mb,
        )
    except PolicyViolationFromServer as exc:
        return _back("/catalog", err=exc.detail)
    except (ClientError, ValueError) as exc:
        return _back("/catalog", err=f"Catalog rejected the offering: {exc}")
    return _back(f"/catalog/{offering_id.strip()}", flash="offering_added")


@router.post("/catalog/{offering_id}/price", response_model=None)
async def add_price(
    offering_id: str,
    price_id: str = Form(...),
    amount: str = Form(...),
    valid_from: str = Form(default=""),
    retire_current: str = Form(default=""),
) -> RedirectResponse:
    back = f"/catalog/{offering_id}"
    try:
        await get_clients().catalog.admin_add_price(
            offering_id,
            price_id=price_id.strip(),
            amount=amount.strip(),
            valid_from=_parse_dt(valid_from),
            retire_current=retire_current == "yes",
        )
    except PolicyViolationFromServer as exc:
        return _back(back, err=exc.detail)
    except (ClientError, ValueError) as exc:
        return _back(back, err=f"Catalog rejected the price: {exc}")
    return _back(back, flash="price_added")


@router.post("/catalog/{offering_id}/window", response_model=None)
async def set_window(
    offering_id: str,
    valid_from: str = Form(default=""),
    valid_to: str = Form(default=""),
) -> RedirectResponse:
    back = f"/catalog/{offering_id}"
    try:
        await get_clients().catalog.admin_set_offering_window(
            offering_id,
            valid_from=_parse_dt(valid_from),
            valid_to=_parse_dt(valid_to),
        )
    except PolicyViolationFromServer as exc:
        return _back(back, err=exc.detail)
    except (ClientError, ValueError) as exc:
        return _back(back, err=f"Catalog rejected the window: {exc}")
    return _back(back, flash="window_set")


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
            "flash": request.query_params.get("flash", ""),
            "err": request.query_params.get("err", "")[:300],
        },
    )
