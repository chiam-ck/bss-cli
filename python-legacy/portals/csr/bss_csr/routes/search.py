"""GET /search — find a customer by name or MSISDN, jump into a session.

Lighter than the v0.5 search route: returns a card list, not a 360
view. Each card has a "Start cockpit session about this customer"
button that POSTs ``/cockpit/new`` with the customer pre-pinned.

MSISDN-shaped queries don't 303 to a 360 (the 360 page is gone in
v0.13); they fall through to the same name-search flow with the
digits as the query, which `crm.find_customer_by_msisdn` resolves
to a card.
"""

from __future__ import annotations

import re

import structlog
from bss_clients.errors import ClientError
from bss_orchestrator.clients import get_clients
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


_MSISDN_RE = re.compile(r"^\+?\d{6,}$")

# Deliberately loose — one @ with something either side. The CRM
# lookup is the validator; the regex only routes the query.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


def _looks_like_msisdn(query: str) -> bool:
    return bool(_MSISDN_RE.match(query.strip()))


def _looks_like_email(query: str) -> bool:
    return bool(_EMAIL_RE.match(query.strip()))


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "") -> HTMLResponse:
    q_clean = q.strip()
    results: list[dict] = []
    clients = get_clients()

    if q_clean and _looks_like_msisdn(q_clean):
        digits = q_clean.lstrip("+").replace(" ", "")
        try:
            cust = await clients.crm.find_customer_by_msisdn(digits)
        except ClientError:
            cust = None
        if cust:
            results = [_flatten_customer(cust)]
    elif q_clean and _looks_like_email(q_clean):
        try:
            cust = await clients.crm.find_customer_by_email(q_clean)
        except ClientError:
            cust = None
        if cust:
            results = [_flatten_customer(cust)]
    elif q_clean:
        try:
            raw = await clients.crm.list_customers(name_contains=q_clean)
        except ClientError:
            raw = []
        results = [_flatten_customer(c) for c in (raw or [])]

    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "active_page": "search",
            "model": "(env default)",
            "q": q_clean,
            "results": results,
        },
    )


@router.post("/search/start_session", response_model=None)
async def search_start_session(
    customer_id: str = Form(...),
) -> RedirectResponse:
    """Open a fresh cockpit session pinned to a customer; 303 to it.

    Mirrors POST /cockpit/new but takes the focus on creation so the
    operator lands on the thread already pinned.
    """
    from bss_cockpit import OPERATOR_ACTOR, Conversation

    label_safe = customer_id[:32]
    conv = await Conversation.open(
        actor=OPERATOR_ACTOR,
        label=f"customer {label_safe}",
        customer_focus=customer_id,
    )
    return RedirectResponse(
        url=f"/cockpit/{conv.session_id}", status_code=303
    )


def _flatten_customer(c: dict) -> dict:
    individual = c.get("individual") or {}
    name = " ".join(
        s for s in [individual.get("givenName"), individual.get("familyName")] if s
    ).strip() or c.get("name", "—")
    email = ""
    msisdn = ""
    for cm in c.get("contactMedium") or []:
        if cm.get("mediumType") == "email" and not email:
            email = cm.get("value", "") or (cm.get("characteristic") or {}).get(
                "emailAddress", ""
            )
        if cm.get("mediumType") == "mobile" and not msisdn:
            msisdn = cm.get("value", "") or (cm.get("characteristic") or {}).get(
                "phoneNumber", ""
            )
    return {
        "id": c.get("id", "?"),
        "name": name,
        "status": c.get("status", "?"),
        "kyc_status": c.get("kycStatus", c.get("kyc_status", "?")),
        "email": email,
        "msisdn": msisdn,
    }
