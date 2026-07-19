"""``/billing/history`` — paginated charge history (v0.10 PR 9).

V0_10_0.md Track 8. Read-only paginated view of the customer's
payment-attempt history.

Doctrine reminders:

* ``customer_id`` from ``request.state.customer_id``; the BSS
  ``payment.list_payments(customer_id=...)`` call is server-side
  scoped to that id, so cross-customer rows can't leak via this
  route.
* No step-up (read-only).
* No portal_action audit row on the success path — non-sensitive
  read.
* Three reads per page render: list_payments (the page), count_payments
  (total for "Page X of Y"), list_methods (to map method_id → last-4
  brand). The phase doc says "no exports / no PDFs / no CSV" — kept
  intentionally narrow.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, Response

from ..clients import get_clients
from ..security import requires_linked_customer
from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


PAGE_SIZE = 20


def _purpose_label(purpose: str) -> str:
    """Map BSS payment.purpose to a customer-facing description."""
    return {
        "subscription": "Subscription renewal",
        "subscription_renewal": "Subscription renewal",
        "subscription_activation": "New line activation",
        "vas": "Add-on / top-up",
        "vas_purchase": "Add-on / top-up",
        "card_change": "Payment method change",
    }.get(purpose, purpose.replace("_", " ").capitalize())


def _last4_index(methods: list[dict[str, Any]]) -> dict[str, str]:
    """Build payment_method_id → last-4 lookup. Removed methods miss.

    The TMF response nests card metadata under ``cardSummary``, not at
    the top level. v0.16 fix — the previous lookup ``m.get("last4")``
    always returned None since v0.10's billing-history page launch,
    making EVERY charge render "(removed)" in the UI.
    """
    out: dict[str, str] = {}
    for m in methods:
        mid = m.get("id")
        if not mid:
            continue
        cs = m.get("cardSummary") or {}
        out[mid] = cs.get("last4") or m.get("last4") or ""
    return out


@router.get("/billing/history", response_class=HTMLResponse)
async def history(
    request: Request,
    page: int = Query(default=0, ge=0, le=10_000),
    customer_id: str = Depends(requires_linked_customer),
) -> Response:
    """Render one page of charges for the verified customer.

    ``page`` is 0-indexed in the URL; the template renders 1-indexed
    counts to the customer ("Page 1 of 7"). Pagination clamps at the
    server: page>last just renders an empty page with a link back to
    page 0.
    """
    clients = get_clients()
    offset = page * PAGE_SIZE

    attempts = await clients.payment.list_payments(
        customer_id=customer_id, limit=PAGE_SIZE, offset=offset
    )
    total = await clients.payment.count_payments(customer_id=customer_id)
    methods = await clients.payment.list_methods(customer_id)
    last4_by_method = _last4_index(methods)

    rows = []
    for a in attempts:
        rows.append({
            "id": a.get("id"),
            "attempted_at": a.get("attemptedAt"),
            "amount": a.get("amount"),
            "currency": a.get("currency") or "SGD",
            "status": a.get("status"),
            "purpose": a.get("purpose"),
            "purpose_label": _purpose_label(a.get("purpose") or ""),
            "method_last4": last4_by_method.get(a.get("paymentMethodId")) or "",
            "method_id": a.get("paymentMethodId"),
            "decline_reason": a.get("declineReason"),
            "gateway_ref": a.get("gatewayRef"),
        })

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    has_prev = page > 0
    has_next = (page + 1) < pages and len(attempts) == PAGE_SIZE

    return templates.TemplateResponse(
        request,
        "billing_history.html",
        {
            "rows": rows,
            "page": page,
            "page_human": page + 1,
            "pages": pages,
            "has_prev": has_prev,
            "has_next": has_next,
            "total": total,
        },
    )
