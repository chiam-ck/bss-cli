"""Shared Jinja2Templates instance + Response helper.

Routes import ``templates`` from here rather than constructing their
own Jinja environments. The loader chain is:
1. portal's own ``templates/`` (per-page templates, plus any local
   override of a shared partial)
2. ``bss_portal_ui``'s ``templates/`` (the agent log widget +
   ``agent_event.html`` partial — shared with portals/csr)
"""

from __future__ import annotations

from pathlib import Path

import bss_branding
from bss_branding import branding_css_block
from bss_models import BSS_RELEASE
from bss_portal_ui import TEMPLATE_DIR as SHARED_TEMPLATE_DIR
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader
from markupsafe import Markup

_LOCAL_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(_LOCAL_TEMPLATE_DIR))
templates.env.loader = ChoiceLoader(
    [
        FileSystemLoader(str(_LOCAL_TEMPLATE_DIR)),
        FileSystemLoader(str(SHARED_TEMPLATE_DIR)),
    ]
)
# v0.14 — every template gets ``bss_release``. v1.8 demoted it from
# the header brand-tag to the footer footnote ("bss-cli vX.Y.Z" —
# product attribution, never rebranded). Single source from
# ``bss_models.BSS_RELEASE``; bump there once per release.
templates.env.globals["bss_release"] = BSS_RELEASE


# v1.8 — operator branding. CALLABLE globals, evaluated per render:
# a static value would freeze the brand at process start and defeat
# the settings.toml mtime hot-reload.
def _branding_style() -> Markup:
    return Markup("<style>" + branding_css_block(bss_branding.current().theme) + "</style>")


templates.env.globals["branding"] = bss_branding.current
templates.env.globals["branding_style"] = _branding_style

# v1.8.0 fix — static-asset cache-buster, stamped at process start.
# Mirrors the cockpit's v1.6.1 asset_v: without it browsers keep a
# stale portal.css across deploys, so a theme change appears to
# half-apply (chrome recolors via the inline :root block while
# stylesheet-driven components keep the cached palette — the "data
# bar is still green" bug). Process wall-clock, not bss_clock: this
# is infrastructure, not business logic.
import time  # noqa: E402

templates.env.globals["asset_v"] = str(int(time.time()))
