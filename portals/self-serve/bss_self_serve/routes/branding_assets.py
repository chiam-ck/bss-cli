"""``GET /branding/logo`` — the operator's uploaded logo image.

Public by design: the header shows the logo to anonymous visitors on
/welcome and /plans, so the path is on the ``PUBLIC_EXACT_PATHS``
allowlist. Bytes + headers come from
:func:`bss_branding.web.logo_response` (404 when no logo is
configured); the URL carries ``?v=<mtime>`` so immutable caching is
safe. No session, no customer data, no BSS write.
"""

from __future__ import annotations

from bss_branding.web import logo_response
from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()


@router.get("/branding/logo")
async def branding_logo() -> Response:
    return logo_response()
