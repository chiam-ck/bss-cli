"""bss-portal-ui — shared UI bits for BSS-CLI portals.

Public API:
- :data:`TEMPLATE_DIR` / :data:`STATIC_DIR` — paths the portal app
  factories pass to ``Jinja2Templates(loader=...)`` and ``StaticFiles``.
- :func:`agent_log.project` / :func:`agent_log.render_html` — turn an
  ``AgentEvent`` from ``bss_orchestrator.session`` into the dict and
  HTML partial the agent log widget consumes.
- :func:`sse.format_frame` / :func:`sse.status_html` — encode SSE
  frames and the status-dot fragment.
- :func:`chat_html.render_assistant_bubble` /
  :func:`chat_html.render_tool_pill` /
  :func:`chat_html.render_chat_markdown` — chat-bubble HTML for the
  v0.12 customer chat + v0.13 operator cockpit chat thread. Both
  surfaces stream the same shape; the renderer is shared so they
  cannot drift apart.

The package owns the ``partials/agent_log.html`` and
``partials/agent_event.html`` templates plus the ``portal_base.css``
and vendored ``htmx.min.js`` / ``htmx-sse.js`` so portals don't
duplicate them.
"""

from __future__ import annotations

from .chat_html import (
    render_assistant_bubble,
    render_chat_markdown,
    render_tool_pill,
    strip_reasoning_leakage,
)
from .paths import STATIC_DIR, TEMPLATE_DIR

__all__ = [
    "STATIC_DIR",
    "TEMPLATE_DIR",
    "render_assistant_bubble",
    "render_chat_markdown",
    "render_tool_pill",
    "strip_reasoning_leakage",
]
