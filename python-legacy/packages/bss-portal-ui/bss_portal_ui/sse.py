"""SSE frame helpers shared across portals.

Both portals (and any future ones) emit Server-Sent Event frames the
same way: one ``event:`` line, one ``data:`` line containing a
single-line HTML partial, terminated by a blank line. Centralising
the encoding keeps escape rules uniform.
"""

from __future__ import annotations


def format_frame(event_name: str, html_line: str) -> bytes:
    """Encode one SSE frame.

    ``html_line`` MUST be a single line — embedded newlines split the
    frame at the wrong boundary. Use ``agent_log.render_html`` (which
    collapses newlines) or pre-collapse the string yourself.
    """
    return f"event: {event_name}\ndata: {html_line}\n\n".encode("utf-8")


_STATUS_CLASSES = {
    "live": "dot live",
    "done": "dot done",
    "error": "dot error",
    "idle": "dot idle",
}


def status_html(status: str) -> str:
    """Tiny fragment for the agent log header status indicator."""
    cls = _STATUS_CLASSES.get(status, "dot idle")
    return f'<span class="{cls}"></span> {status}'
