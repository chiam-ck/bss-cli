"""Public legal pages — /terms and /privacy.

Both routes live in ``security.PUBLIC_EXACT_PATHS`` so the session
middleware doesn't redirect anonymous visitors. They render
static-text Jinja templates; no BSS reads, no auth, no LLM. The
content is placeholder template copy — replace with the operator's
real legal text when shipping to a real cohort.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..templating import templates

router = APIRouter()


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "legal_terms.html", {})


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "legal_privacy.html", {})
