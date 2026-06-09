"""Subscription detail — balances, services, usage, eSIM (v1.6 cockpit CRM).

Read-only. Terminate is destructive, plan change and renew-now and VAS
top-ups move money — all four render as "Ask the agent" handoffs so the
conversation keeps its propose-then-``/confirm`` chokepoint. The eSIM
panel is the v0.10 read-only re-display (NOT a SGP.22 rearm — see
DECISIONS 2026-04-27).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from bss_clients.errors import ClientError
from bss_orchestrator.clients import get_clients
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..templating import templates
from ..views import balance_rows, customer_name, field, fmt_dt

log = structlog.get_logger(__name__)
router = APIRouter()


async def _best_effort(coro) -> Any:
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 — sections degrade independently
        log.warning("csr.subscription.section_failed", error=str(exc))
        return None


@router.get("/subscriptions/{subscription_id}", response_class=HTMLResponse)
async def subscription_detail(
    request: Request, subscription_id: str
) -> HTMLResponse:
    clients = get_clients()
    try:
        sub = await clients.subscription.get(subscription_id)
    except ClientError as exc:
        if exc.status_code == 404:
            raise HTTPException(404, f"Subscription {subscription_id} not found")
        raise

    customer_id = field(sub, "customer_id", default="")
    offering_id = field(sub, "offering_id", default="")

    cust, offering, services, usage, esim = await asyncio.gather(
        _best_effort(clients.crm.get_customer(customer_id)) if customer_id else _noop(),
        _best_effort(clients.catalog.get_offering(offering_id)) if offering_id else _noop(),
        _best_effort(clients.som.list_services_for_subscription(subscription_id)),
        _best_effort(
            clients.mediation.list_usage(subscription_id=subscription_id, limit=15)
        ),
        _best_effort(clients.subscription.get_esim_activation(subscription_id)),
    )

    usage_views = [
        {
            "at": fmt_dt(field(u, "event_time", "occurred_at", default="")),
            "type": field(u, "event_type", "type", default="—"),
            "quantity": f"{field(u, 'quantity', default='?')} {field(u, 'unit', default='')}".strip(),
            "roaming": bool(field(u, "roaming_indicator", default=False)),
        }
        for u in usage or []
    ]

    service_views = [
        {
            "id": s.get("id", "?"),
            "type": field(s, "type", "service_type", default="—"),
            "spec_id": field(s, "spec_id", default=""),
            "state": field(s, "state", default="?"),
        }
        for s in services or []
    ]

    price_bits = []
    amount = field(sub, "effective_amount", "price_amount", default=None)
    if amount is not None:
        price_bits.append(f"{field(sub, 'price_currency', default='SGD')} {amount}")
    if field(sub, "promo_code", default=""):
        price_bits.append(f"promo {field(sub, 'promo_code')}")

    return templates.TemplateResponse(
        request,
        "subscription_detail.html",
        {
            "active_page": "customers",
            "model": "(env default)",
            "sub": {
                "id": sub.get("id", subscription_id),
                "state": field(sub, "state", default="?"),
                "state_reason": field(sub, "state_reason", default=""),
                "msisdn": sub.get("msisdn", "—"),
                "iccid": sub.get("iccid", ""),
                "customer_id": customer_id,
                "customer_name": customer_name(cust),
                "offering_id": offering_id,
                "offering_name": (offering or {}).get("name", ""),
                "price": " · ".join(price_bits) or "—",
                "activated_at": fmt_dt(field(sub, "activated_at", default="")),
                "period_end": fmt_dt(field(sub, "current_period_end", default="")),
                "next_renewal": fmt_dt(field(sub, "next_renewal_at", default="")),
                "pending_offering_id": field(sub, "pending_offering_id", default=""),
                "pending_effective_at": fmt_dt(field(sub, "pending_effective_at", default="")),
            },
            "balances": balance_rows(sub.get("balances")),
            "services": service_views,
            "usage": usage_views,
            "esim": {
                "iccid": (esim or {}).get("iccid", ""),
                "activation_code": field(esim or {}, "activation_code", default=""),
            } if esim else None,
        },
    )


async def _noop() -> None:
    return None
