"""ASCII renderers for the port-request (MNP) tools."""

from __future__ import annotations

from typing import Any


def render_port_request_list(payload: list[dict[str, Any]]) -> str:
    if not payload:
        return "(no port requests)"
    rows: list[str] = ["── Port Requests " + "─" * 50, ""]
    rows.append(
        f"  {'ID':<14}  {'Direction':<10}  {'Donor MSISDN':<14}  "
        f"{'Carrier':<16}  {'State':<10}  Requested"
    )
    rows.append(
        f"  {'─' * 14}  {'─' * 10}  {'─' * 14}  {'─' * 16}  "
        f"{'─' * 10}  {'─' * 10}"
    )
    for p in payload[:50]:
        rows.append(
            f"  {p.get('id', '?'):<14}  "
            f"{p.get('direction', '?'):<10}  "
            f"{p.get('donorMsisdn') or p.get('donor_msisdn') or '—':<14}  "
            f"{(p.get('donorCarrier') or p.get('donor_carrier') or '—')[:16]:<16}  "
            f"{p.get('state', '?'):<10}  "
            f"{(p.get('requestedPortDate') or p.get('requested_port_date') or '')[:10]}"
        )
    if len(payload) > 50:
        rows.append(f"  (+ {len(payload) - 50} more)")
    rows.append("")
    rows.append(f"  ({len(payload)} rows shown — pass `limit` to widen.)")
    return "\n".join(rows)


def render_port_request_get(payload: dict[str, Any]) -> str:
    title = f"── Port Request {payload.get('id', '?')} "
    rows = [title + "─" * max(0, 60 - len(title)), ""]
    rows.append(f"  Direction       : {payload.get('direction', '?')}")
    rows.append(
        f"  Donor MSISDN    : "
        f"{payload.get('donorMsisdn') or payload.get('donor_msisdn') or '—'}"
    )
    rows.append(
        f"  Donor carrier   : "
        f"{payload.get('donorCarrier') or payload.get('donor_carrier') or '—'}"
    )
    rows.append(
        f"  Target sub      : "
        f"{payload.get('targetSubscriptionId') or payload.get('target_subscription_id') or '—'}"
    )
    rows.append(
        f"  Requested date  : "
        f"{payload.get('requestedPortDate') or payload.get('requested_port_date') or '—'}"
    )
    rows.append(f"  State           : {payload.get('state', '?')}")
    if payload.get("rejectionReason") or payload.get("rejection_reason"):
        rows.append(
            f"  Rejection reason: "
            f"{payload.get('rejectionReason') or payload.get('rejection_reason')}"
        )
    rows.append(
        f"  Created         : "
        f"{(payload.get('createdAt') or payload.get('created_at') or '')[:19]}"
    )
    rows.append(
        f"  Updated         : "
        f"{(payload.get('updatedAt') or payload.get('updated_at') or '')[:19]}"
    )
    return "\n".join(rows)
