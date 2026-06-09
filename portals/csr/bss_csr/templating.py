"""Shared Jinja2Templates instance for the CSR portal.

Loader chain:
1. portal's own ``templates/`` (per-page layouts, partials, plus
   any local override of a shared partial)
2. ``bss_portal_ui``'s ``templates/`` (agent log widget +
   ``agent_event.html`` partial — shared with portals/self-serve)
"""

from __future__ import annotations

from pathlib import Path

from bss_models import BSS_RELEASE
from bss_portal_ui import TEMPLATE_DIR as SHARED_TEMPLATE_DIR
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

_LOCAL_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(_LOCAL_TEMPLATE_DIR))
templates.env.loader = ChoiceLoader(
    [
        FileSystemLoader(str(_LOCAL_TEMPLATE_DIR)),
        FileSystemLoader(str(SHARED_TEMPLATE_DIR)),
    ]
)
# v0.14 — every template gets ``bss_release`` for the brand-tag.
templates.env.globals["bss_release"] = BSS_RELEASE

# v1.6 — CRM screens share the lenient payload helpers as filters so
# templates can badge states and format timestamps without per-route
# plumbing.
from .views import fmt_dt, state_tone  # noqa: E402

templates.env.filters["fmt_dt"] = fmt_dt
templates.env.filters["tone"] = state_tone
