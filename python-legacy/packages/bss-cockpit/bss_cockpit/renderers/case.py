"""Case renderer — case header, child tickets, notes."""

from __future__ import annotations

from typing import Any

from ._box import box


def render_case(
    case: dict[str, Any],
    *,
    tickets: list[dict[str, Any]] | None = None,
    notes: list[dict[str, Any]] | None = None,
) -> str:
    """Render a case with its child tickets and notes."""
    cid = case.get("id", "CASE-???")
    subject = case.get("subject", "")
    state = case.get("state", "?")
    priority = case.get("priority", "?")

    cust_id = case.get("customerId", "—")
    opened_at = case.get("createdAt", "—")
    opened_by = case.get("openedBy") or case.get("createdBy") or "—"

    tickets = tickets or []
    notes = notes or []

    lines = [
        f"Customer: {cust_id}",
        f"Opened:   {opened_at} by {opened_by}",
        "",
        f"── Tickets ({len(tickets)}) " + "─" * 38,
    ]
    if not tickets:
        lines.append("(none)")
    for t in tickets:
        agent = t.get("assignedAgent") or t.get("agentId", "—")
        lines.append(
            f"{t.get('id'):<8} {t.get('ticketType', ''):<18} "
            f"{t.get('state', ''):<14} {t.get('priority', ''):<6} {agent}"
        )
    lines.append("")
    lines.append(f"── Notes ({len(notes)}) " + "─" * 40)
    if not notes:
        lines.append("(none)")
    for n in notes:
        lines.append(
            f"[{n.get('authorId', '—')} {n.get('createdAt', '')[:16]}] "
            f"{n.get('body', '')[:60]}"
        )

    title = f"{cid}  {subject!r:<40}  [{state}]  {priority}"
    return box(lines, title=title, width=72)
