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
# v0.14 — every template gets ``bss_release``. v1.8 demoted it from
# the header brand-tag to the footer footnote (product attribution).
templates.env.globals["bss_release"] = BSS_RELEASE

# v1.8 — operator branding. CALLABLE globals, evaluated per render so
# the settings.toml mtime hot-reload is visible on the next refresh.
import bss_branding  # noqa: E402
from bss_branding import branding_css_block  # noqa: E402
from markupsafe import Markup  # noqa: E402


def _branding_style() -> Markup:
    return Markup("<style>" + branding_css_block(bss_branding.current().theme) + "</style>")


templates.env.globals["branding"] = bss_branding.current
templates.env.globals["branding_style"] = _branding_style

# v1.6.1 — static-asset cache-buster, stamped at process start. Safari
# (iPad especially) caches CSS/JS aggressively across deploys; a fresh
# query param per container boot forces a refetch after every rebuild.
# Process wall-clock, not bss_clock: this is infrastructure, not
# business logic.
import time  # noqa: E402

templates.env.globals["asset_v"] = str(int(time.time()))

# v1.6 — CRM screens share the lenient payload helpers as filters so
# templates can badge states and format timestamps without per-route
# plumbing.
from .views import fmt_dt, state_tone  # noqa: E402

templates.env.filters["fmt_dt"] = fmt_dt
templates.env.filters["tone"] = state_tone
