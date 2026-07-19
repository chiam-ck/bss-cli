"""Ticket renderers — simple table for ticket lists, single-ticket view."""

from __future__ import annotations

from typing import Any

from ._box import box


def render_ticket(ticket: dict[str, Any]) -> str:
    """Render a single ticket view (header + fields)."""
    tid = ticket.get("id", "TKT-???")
    ttype = ticket.get("ticketType", "?")
    state = ticket.get("state", "?")
    priority = ticket.get("priority", "?")
    subject = ticket.get("subject", "")
    agent = ticket.get("assignedAgent") or "—"
    case = None
    for r in ticket.get("relatedEntity") or []:
        if r.get("entityType") == "case":
            case = r.get("id")
    lines = [
        f"Subject:   {subject}",
        f"Type:      {ttype}",
        f"State:     {state}",
        f"Priority:  {priority}",
        f"Assigned:  {agent}",
        f"Case:      {case or '—'}",
    ]
    return box(lines, title=f"{tid}", width=64)
