"""Operator-cockpit /settings/branding page (v1.8).

The visual front door for operator branding: brand name, theme picker
(palette swatches), logo mark (built-in glyphs or a custom 1-3 char
mark), logo image upload, and an HTMX live preview that writes
nothing until Save.

Doctrine:

* These are NON-destructive config writes — the same class as the
  /settings POSTs, not ``DESTRUCTIVE_TOOLS``/money verbs — so there is
  deliberately no two-step confirm panel.
* One POST → one ``bss_cockpit.config`` writer. The writers are the
  validation gate (tomlkit round-trip + whole-document Pydantic pass);
  this module never touches settings.toml or the logo file directly.
* Upload security: the cap is enforced by BYTES READ (Content-Length
  is browser-asserted fiction), the type by magic bytes inside
  ``write_branding_logo`` (PNG/JPEG/WebP — never SVG), and the
  destination filename is fixed. Logs carry size + type only.
"""

from __future__ import annotations

from typing import Any

import bss_branding
import structlog
from bss_branding import (
    DEFAULT_BRAND_NAME,
    DEFAULT_THEME_ID,
    LOGO_MARKS,
    MAX_LOGO_BYTES,
    THEMES,
    BrandingSettings,
)
from bss_cockpit import (
    remove_branding_logo,
    write_branding_logo,
    write_branding_settings,
)
from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


def _context(
    *,
    flash: str | None = None,
    error: str | None = None,
    error_section: str | None = None,
    form: dict[str, str] | None = None,
) -> dict[str, Any]:
    # Form values come from the FILE (never env-overridden view) so an
    # env override can't get silently baked in on the next save.
    saved = bss_branding.file_settings()
    values = form or {
        "brand_name": saved.brand_name,
        "theme": saved.theme,
        "mark": saved.mark,
    }
    return {
        "active_page": "branding",
        "themes": list(THEMES.values()),
        "logo_marks": LOGO_MARKS,
        "values": values,
        "mark_is_custom": values["mark"] not in LOGO_MARKS,
        "logo_view": bss_branding.current(),
        "max_logo_kb": MAX_LOGO_BYTES // 1024,
        # The initial (non-HTMX) render of partials/branding_preview.html.
        "t": THEMES.get(values["theme"], THEMES[DEFAULT_THEME_ID]),
        "name": values["brand_name"],
        "mark": values["mark"],
        "flash": flash,
        "error": error,
        "error_section": error_section,
    }


def _resolve_mark(mark_choice: str, mark_custom: str) -> str:
    return mark_custom.strip() if mark_choice == "custom" else mark_choice


@router.get("/settings/branding", response_class=HTMLResponse)
async def branding_page(request: Request, flash: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "branding.html", _context(flash=flash))


@router.post("/settings/branding", response_model=None)
async def branding_save(
    request: Request,
    brand_name: str = Form(...),
    theme: str = Form(...),
    mark_choice: str = Form(...),
    mark_custom: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    mark = _resolve_mark(mark_choice, mark_custom)
    saved = bss_branding.file_settings()
    try:
        update = BrandingSettings(
            brand_name=brand_name,
            theme=theme,
            mark=mark,
            logo_image=saved.logo_image,  # preserved — logo has its own forms
        )
        write_branding_settings(update)
    except (ValidationError, ValueError) as exc:
        return templates.TemplateResponse(
            request,
            "branding.html",
            _context(
                error=str(exc),
                error_section="branding",
                form={"brand_name": brand_name, "theme": theme, "mark": mark},
            ),
            status_code=400,
        )
    log.info("cockpit.branding.saved", theme=theme)
    return RedirectResponse(url="/settings/branding?flash=branding_saved", status_code=303)


@router.post("/settings/branding/logo", response_model=None)
async def branding_logo_upload(request: Request, logo: UploadFile) -> HTMLResponse | RedirectResponse:
    # Read one byte past the cap: if we got cap+1 bytes the file is
    # oversize regardless of what Content-Length claimed.
    data = await logo.read(MAX_LOGO_BYTES + 1)
    try:
        write_branding_logo(data)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "branding.html",
            _context(error=str(exc), error_section="logo"),
            status_code=400,
        )
    return RedirectResponse(url="/settings/branding?flash=logo_saved", status_code=303)


@router.post("/settings/branding/logo/delete", response_model=None)
async def branding_logo_delete(request: Request) -> RedirectResponse:
    remove_branding_logo()
    return RedirectResponse(url="/settings/branding?flash=logo_removed", status_code=303)


@router.get("/settings/branding/preview", response_class=HTMLResponse)
async def branding_preview(
    request: Request,
    theme: str = "",
    brand_name: str = "",
    mark_choice: str = "$",
    mark_custom: str = "",
) -> HTMLResponse:
    """HTMX fragment — renders the preview card from form state
    WITHOUT writing anything. Unknown/blank values degrade to
    defaults so half-typed input never 4xxes the fragment."""
    mark = _resolve_mark(mark_choice, mark_custom) or "$"
    return templates.TemplateResponse(
        request,
        "partials/branding_preview.html",
        {
            "t": THEMES.get(theme, THEMES[DEFAULT_THEME_ID]),
            "name": brand_name.strip() or DEFAULT_BRAND_NAME,
            "mark": mark[:3],
        },
    )
