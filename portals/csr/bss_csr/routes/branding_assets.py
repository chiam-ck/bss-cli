"""``GET /branding/logo`` — the operator's uploaded logo image.

Bytes + headers come from :func:`bss_branding.web.logo_response`
(404 when no logo is configured). The layout links this URL with a
``?v=<mtime>`` cache-buster, so the immutable cache headers are safe.
"""

from __future__ import annotations

from bss_branding.web import logo_response
from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()


@router.get("/branding/logo")
async def branding_logo() -> Response:
    return logo_response()
