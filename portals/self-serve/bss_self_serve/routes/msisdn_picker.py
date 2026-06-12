"""MSISDN picker — /signup/{plan}/msisdn

A small step inserted between plan selection and the signup form so
customers pick their own number instead of having one auto-assigned
during SOM decomposition. This is a READ endpoint — the number is
actually reserved later when ``order.create`` runs through SOM with
``msisdn_preference`` pointing at the chosen value.

We show 12 available numbers (three rows of four on desktop). A
``?prefix=`` query param lets the scenario runner pin to a specific
range. Older numbers (assigned / reserved / recycled) are filtered
out; only ``status == "available"`` lands on the page.

Race note: a number shown as available here could get reserved by
another signup in the ~30-second window between render and
``order.create``. In that case SOM's ``reserve_next_msisdn`` still
succeeds — it falls back to the next available number and stores
the mismatch on the subscription. The portal does not try to
lock-and-hold, by design; it's a demo.
"""

from __future__ import annotations

from bss_portal_auth import IdentityView
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ..clients import get_clients
from ..offerings import find_plan, flatten_offerings
from ..security import requires_verified_email
from ..templating import templates

router = APIRouter()


@router.get("/signup/{plan_id}/msisdn", response_class=HTMLResponse)
async def msisdn_picker(
    request: Request,
    plan_id: str,
    prefix: str | None = Query(default=None, pattern=r"^[0-9]+$"),
    limit: int = Query(default=12, ge=1, le=40),
    _identity: IdentityView = Depends(requires_verified_email),
) -> HTMLResponse:
    clients = get_clients()
    plan = find_plan(flatten_offerings(await clients.catalog.list_offerings()), plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Unknown plan: {plan_id}")

    numbers_raw = await clients.inventory.list_msisdns(
        state="available",
        prefix=prefix,
        limit=limit,
    )
    numbers = [_format_number(n["msisdn"]) for n in numbers_raw]

    return templates.TemplateResponse(
        request,
        "msisdn_picker.html",
        {
            "plan": plan,
            "numbers": numbers,
            "prefix": prefix or "",
        },
    )


def _format_number(msisdn: str) -> dict[str, str]:
    """Present '90000002' as '9000 0002' for readability; keep the raw form for routing."""
    display = msisdn
    if len(msisdn) == 8 and msisdn.isdigit():
        display = f"{msisdn[:4]} {msisdn[4:]}"
    return {"raw": msisdn, "display": display}
