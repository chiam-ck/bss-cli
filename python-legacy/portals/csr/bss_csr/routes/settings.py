"""Operator-cockpit /settings page (v0.13 PR8).

Two textareas backed by ``.bss-cli/OPERATOR.md`` and
``.bss-cli/settings.toml``. ``GET /settings`` shows both files; POST
handlers persist + validate via :mod:`bss_cockpit.config` helpers and
303 back to the form. Invalid TOML / Pydantic errors return 400 with
the parser's diagnostic message echoed in-page.

No auth — same single-operator-by-design contract as the rest of the
cockpit. ``actor`` for any audit trail is hardcoded to
``bss_cockpit.OPERATOR_ACTOR`` (v0.13.1).

Doctrine-coupled: this is the only write path to either file outside
the REPL's ``/operator edit`` and ``/config edit`` slash commands.
``write_operator_md`` and ``write_settings_toml`` are the validation
gate — bypassing them risks operator typos bricking the cockpit.
"""

from __future__ import annotations

import tomllib
from typing import Any

import structlog
from bss_cockpit import (
    OPERATOR_ACTOR,
    write_operator_md,
    write_settings_toml,
)
from bss_cockpit import (
    current as cockpit_config_current,
)
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ..templating import templates

log = structlog.get_logger(__name__)
router = APIRouter()


def _settings_context(
    *,
    error: str | None = None,
    error_section: str | None = None,
    flash: str | None = None,
) -> dict[str, Any]:
    cfg = cockpit_config_current()
    return {
        "actor": OPERATOR_ACTOR,
        "model": cfg.settings.llm.model or "(env default)",
        "operator_md": cfg.operator_md,
        "settings_toml": cfg.settings_path.read_text(encoding="utf-8"),
        "operator_md_path": str(cfg.operator_md_path),
        "settings_path": str(cfg.settings_path),
        "last_loaded_at": cfg.last_loaded_at.strftime("%Y-%m-%d %H:%M:%S"),
        "error": error,
        "error_section": error_section,
        "flash": flash,
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    flash: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "settings.html", _settings_context(flash=flash)
    )


@router.post("/settings/operator", response_model=None)
async def settings_save_operator_md(
    request: Request,
    operator_md: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    try:
        write_operator_md(operator_md)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                **_settings_context(
                    error=str(exc), error_section="operator"
                ),
                "operator_md": operator_md,  # echo unsaved input
            },
            status_code=400,
        )
    log.info("cockpit.settings.operator_md_saved", actor=OPERATOR_ACTOR)
    return RedirectResponse(url="/settings?flash=operator_saved", status_code=303)


@router.post("/settings/config", response_model=None)
async def settings_save_config_toml(
    request: Request,
    settings_toml: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    try:
        write_settings_toml(settings_toml)
    except (tomllib.TOMLDecodeError, ValidationError, ValueError) as exc:
        # Echo the raw text back so the operator's draft isn't lost
        # to the round-trip.
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                **_settings_context(
                    error=f"{type(exc).__name__}: {exc}",
                    error_section="config",
                ),
                "settings_toml": settings_toml,
            },
            status_code=400,
        )
    log.info("cockpit.settings.toml_saved")
    return RedirectResponse(url="/settings?flash=config_saved", status_code=303)
