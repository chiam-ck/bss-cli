"""Customer 360 renderer — KYC, contact, subscriptions, cases, interactions."""

from __future__ import annotations

from typing import Any

from ._box import box, format_msisdn, state_dot


def _contact_line(contact_mediums: list[dict[str, Any]]) -> str:
    email = None
    phone = None
    for cm in contact_mediums or []:
        ch = cm.get("characteristic", {}) or {}
        if cm.get("mediumType") == "email":
            email = ch.get("emailAddress") or cm.get("value")
        elif cm.get("mediumType") == "mobile":
            phone = ch.get("phoneNumber") or cm.get("value")
    parts = [p for p in (email, phone) if p]
    return " · ".join(parts) if parts else "—"


def _kyc_badge(customer: dict[str, Any]) -> str:
    """Compact ✓ KYC inline next to the customer name."""
    status = (customer.get("kycStatus") or customer.get("kyc_status") or "").lower()
    if status == "verified":
        return " · ✓ KYC verified"
    if status in ("not_verified", "pending"):
        return " · ⚠ KYC not verified"
    return ""


def _section(title: str, count: int | None = None, *, width: int = 60) -> str:
    suffix = f" ({count})" if count is not None else ""
    label = title + suffix
    return f"── {label} " + "─" * max(2, width - len(label) - 4)


def _bundle_pct(s: dict[str, Any]) -> str:
    """Compact `bundle 42%` string for the data row of a sub."""
    for b in s.get("balances") or []:
        if b.get("type") == "data" and b.get("total"):
            try:
                used = float(b.get("used", 0))
                total = float(b["total"])
                if total:
                    return f"  bundle {int(used / total * 100)}%"
            except (TypeError, ValueError):
                pass
    return ""


def render_customer_360(
    customer: dict[str, Any],
    *,
    subscriptions: list[dict[str, Any]] | None = None,
    cases: list[dict[str, Any]] | None = None,
    tickets_by_case: dict[str, list[dict[str, Any]]] | None = None,
    interactions: list[dict[str, Any]] | None = None,
    interactions_limit: int = 5,
) -> str:
    """Render the customer 360 hero view (CLI counterpart to the CSR portal 360)."""
    cid = customer.get("id", "CUST-???")
    name = customer.get("name", "—")
    status = customer.get("status", "unknown")
    since = (customer.get("createdAt") or customer.get("since") or "—")[:10]
    contact = _contact_line(customer.get("contactMedium") or [])
    kyc = _kyc_badge(customer)

    subscriptions = subscriptions or []
    cases = cases or []
    tickets_by_case = tickets_by_case or {}
    interactions = interactions or []

    lines: list[str] = []
    lines.append(f"Status:  {state_dot(status)}{kyc}")
    lines.append(f"Contact: {contact}")
    lines.append(f"Since:   {since}")
    lines.append("")

    # Subscriptions — one compact card per sub
    lines.append(_section("Subscriptions", len(subscriptions)))
    if not subscriptions:
        lines.append("  (none)")
    for s in subscriptions:
        msisdn = format_msisdn(s.get("msisdn", ""))
        bundle = _bundle_pct(s)
        sub_state = s.get("state", "?")
        marker = "⚠ " if sub_state in ("blocked", "suspended") else "  "
        lines.append(
            f"{marker}{s.get('id', '?'):<10} {s.get('offeringId', '?'):<8} "
            f"{sub_state:<10} {msisdn}{bundle}"
        )
    lines.append("")

    # Cases — open cases listed; resolved/closed collapsed to a count
    open_cases = [c for c in cases if c.get("state") not in ("closed", "resolved")]
    closed_count = len(cases) - len(open_cases)
    lines.append(_section("Open Cases", len(open_cases)))
    if not open_cases:
        lines.append("  (none)")
    for c in open_cases:
        subject = (c.get("subject") or "(no subject)")[:34]
        lines.append(
            f"  {c.get('id'):<10} {subject:<34} "
            f"{c.get('priority', ''):<6} {c.get('state', '')}"
        )
        for t in tickets_by_case.get(c.get("id"), []):
            lines.append(
                f"    └─ {t.get('id'):<8} {t.get('ticketType', ''):<14} "
                f"{t.get('priority', ''):<6} {t.get('state', '')}"
            )
    if closed_count:
        lines.append(f"  (+ {closed_count} resolved/closed)")
    lines.append("")

    # Recent interactions
    lines.append(_section("Recent Interactions", len(interactions)))
    if not interactions:
        lines.append("  (none)")
    for it in interactions[:interactions_limit]:
        when = (it.get("createdAt") or it.get("occurredAt") or "")[:16].replace("T", " ")
        chan = (it.get("channel") or "")[:14]
        action = (it.get("action") or it.get("summary") or "")[:34]
        lines.append(f"  {when:<16}  {chan:<14}  {action}")
    if len(interactions) > interactions_limit:
        lines.append(
            f"  (+ {len(interactions) - interactions_limit} more — "
            f"--interactions N to widen)"
        )

    title = f"{cid}  {name}"
    return box(lines, title=title, width=70)
