"""ASCII renderers for the MSISDN/eSIM pool tools.

Both surfaces (REPL + browser veneer) feed tool_result rows through
``render_tool_result`` (in ``.dispatch``) so the visible output is
deterministic — never a markdown table fabricated by the LLM. See
DECISIONS 2026-05-04.
"""

from __future__ import annotations

from typing import Any


def render_msisdn_list(payload: list[dict[str, Any]]) -> str:
    if not payload:
        return "(no MSISDNs match)"
    rows: list[str] = ["── MSISDNs " + "─" * 50, ""]
    rows.append(f"  {'MSISDN':<14}  {'Status':<12}  {'Subscription':<14}  Reserved at")
    rows.append(f"  {'─' * 14}  {'─' * 12}  {'─' * 14}  {'─' * 19}")
    for m in payload[:50]:
        reserved = (m.get("reserved_at") or m.get("reservedAt") or "")[:19]
        rows.append(
            f"  {m.get('msisdn', '?'):<14}  {m.get('status', '?'):<12}  "
            f"{(m.get('assigned_to_subscription_id') or m.get('assignedToSubscriptionId') or '—'):<14}  "
            f"{reserved}"
        )
    if len(payload) > 50:
        rows.append(f"  (+ {len(payload) - 50} more)")
    rows.append("")
    rows.append(
        f"  ({len(payload)} rows shown — call `inventory.msisdn.count` "
        "for the full pool total, or pass a higher `limit` to widen.)"
    )
    return "\n".join(rows)


def render_msisdn_count(payload: dict[str, Any]) -> str:
    pfx = payload.get("prefix")
    title = "── MSISDN pool" + (f" — prefix={pfx}" if pfx else "") + " "
    rows = [title + "─" * max(0, 60 - len(title)), ""]
    rows.append(f"  {'available':<12}  {payload.get('available', 0):>6}")
    rows.append(f"  {'reserved':<12}  {payload.get('reserved', 0):>6}")
    rows.append(f"  {'assigned':<12}  {payload.get('assigned', 0):>6}")
    rows.append(f"  {'ported_out':<12}  {payload.get('ported_out', 0):>6}")
    rows.append(f"  {'─' * 12}  {'─' * 6}")
    rows.append(f"  {'total':<12}  {payload.get('total', 0):>6}")
    return "\n".join(rows)
