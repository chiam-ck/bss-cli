"""Public landing pages — /welcome and /plans.

Both routes are explicitly in ``security.PUBLIC_EXACT_PATHS`` so the
session middleware doesn't redirect anonymous visitors. CTAs route
to ``/auth/login?next=...`` for unauthenticated viewers; signed-in
viewers can click straight through to the gated funnel.

Doctrine (V0_8_0.md §3.2):
* ``/welcome`` is the public marketing surface — sign-in / get-started.
* ``/plans`` lists active offerings and lets the visitor pick one.
  The "Choose this plan" CTA goes to ``/auth/login?next=/signup/<plan>/msisdn``
  for anonymous viewers; the gated route raises ``RedirectToLogin``
  with the same next path, so we don't need template branching.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..clients import get_clients
from ..offerings import flatten_offerings
from ..templating import templates

router = APIRouter()


@router.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "welcome.html",
        {
            "is_signed_in": getattr(request.state, "session", None) is not None,
        },
    )


@router.get("/plans", response_class=HTMLResponse)
async def plans(request: Request) -> HTMLResponse:
    """Public catalog browse. Anonymous visitors see prices + CTAs that
    bounce through /auth/login; signed-in visitors hit the gated flow
    directly."""
    clients = get_clients()
    raw = await clients.catalog.list_offerings()
    plans_list = flatten_offerings(raw)
    return templates.TemplateResponse(
        request,
        "plans.html",
        {
            "plans": plans_list,
            "is_signed_in": getattr(request.state, "session", None) is not None,
        },
    )
