"""GET /api/session/{session_id} — JSON projection of the signup session.

Read-only surface used by the scenario runner's HTTP step type. The
browser UI never needs this (HTMX receives everything via SSE frames
and page navigations); this exists so integration/regression
scenarios can poll for ``done=true`` and capture the resulting IDs
without scraping HTML.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api")


@router.get("/session/{session_id}")
async def session_status(request: Request, session_id: str) -> JSONResponse:
    store = request.app.state.session_store
    sig = await store.get(session_id)
    if sig is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session.")
    return JSONResponse(
        {
            "session_id": sig.session_id,
            "plan": sig.plan,
            "msisdn_preference": sig.msisdn,
            "done": sig.done,
            "error": sig.error,
            "customer_id": sig.customer_id,
            "order_id": sig.order_id,
            "subscription_id": sig.subscription_id,
            "activation_code": sig.activation_code,
        }
    )
