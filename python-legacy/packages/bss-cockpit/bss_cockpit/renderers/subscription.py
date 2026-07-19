"""Subscription hero renderer — the flagship ASCII view."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ._box import box, double_box, format_msisdn, progress_bar, state_dot

_UNLIMITED_UNITS = {"unlimited", "unlim"}


def _fmt_balance(used: float, total: float | None, unit: str) -> str:
    """One balance row: bar + numeric + percent, all column-aligned."""
    if total is None or (isinstance(total, str) and total in _UNLIMITED_UNITS):
        return f"{progress_bar(0, None)}  unlimited"
    pct = 0 if not total else int(round((used / total) * 100))
    nums = f"{used:>6.1f} / {total:>6.1f} {unit.upper():<3}"
    return f"{progress_bar(used, total)}  {nums}  {pct:>3}%"


def _days_to(dt_str: str | None, now: datetime | None = None) -> str:
    if not dt_str:
        return "—"
    try:
        then = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return dt_str
    now = now or datetime.now(then.tzinfo or timezone.utc)  # noqa: bss-clock — render-time wall clock, not transactional
    delta = then - now
    days = delta.days
    return f"{days} days ({then.date().isoformat()})"


def _vas_history_table(history: list[dict[str, Any]]) -> list[str]:
    """Right-aligned amount column, left-aligned dates."""
    if not history:
        return []
    rows = ["── VAS Top-up History " + "─" * 36, ""]
    rows.append(f"  {'Date':<10}  {'Offering':<14}  {'Amount':>9}")
    rows.append(f"  {'─' * 10}  {'─' * 14}  {'─' * 9}")
    for entry in history[:5]:  # 5 most recent
        date = (entry.get("purchasedAt") or entry.get("date") or "—")[:10]
        offering = (entry.get("vasOfferingId") or entry.get("offering") or "—")[:14]
        amount = entry.get("amount") or entry.get("price") or ""
        amount_str = f"SGD {amount:>4}" if amount else "—"
        rows.append(f"  {date:<10}  {offering:<14}  {amount_str:>9}")
    return rows


def render_subscription(
    sub: dict[str, Any],
    *,
    customer: dict[str, Any] | None = None,
    offering: dict[str, Any] | None = None,
    esim: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """Render the subscription hero view.

    Active subscriptions get a single-rule frame (``box``); blocked
    subscriptions get a double-rule frame (``double_box``) so the visual
    weight tells the story before the state label is read. v0.6 polish.
    """
    sub_id = sub.get("id", "SUB-???")
    cust_id = sub.get("customerId", "—")
    cust_name = (customer or {}).get("name", "—")
    msisdn = format_msisdn(sub.get("msisdn", ""))
    plan_name = (offering or {}).get("name", "—")
    plan_id = sub.get("offeringId", "—")
    price = (offering or {}).get("price")
    price_str = f" — SGD {price}/mo" if price else ""
    state = sub.get("state", "unknown")

    activated = sub.get("activatedAt") or sub.get("startDate") or "—"
    next_renewal = sub.get("nextRenewalAt") or sub.get("endDate")

    balances = sub.get("balances") or sub.get("bundleBalances") or []
    rows: list[str] = []
    for b in balances:
        # Accept both shapes: (type/used) from the renderer's original
        # tests, and (allowanceType/consumed/remaining/total) from the
        # live subscription service payload. ``-1`` total is the
        # unlimited sentinel.
        label = str(b.get("type") or b.get("allowanceType") or "?").title()
        used_val = b.get("used")
        if used_val is None and "consumed" in b:
            used_val = b.get("consumed")
        if used_val is None and "remaining" in b and b.get("total") not in (None, -1):
            used_val = float(b["total"]) - float(b["remaining"])
        used = float(used_val or 0)
        total = b.get("total")
        if total in (None, "unlimited", -1):
            total_val: float | None = None
        else:
            total_val = float(total)
        unit = b.get("unit", "")
        rows.append(f"  {label:<7} {_fmt_balance(used, total_val, unit)}")

    if not rows:
        rows = ["  (no bundle balances)"]

    lines = [
        f"Customer:  {cust_name} ({cust_id})",
        f"MSISDN:    {msisdn}",
        f"Plan:      {plan_name} ({plan_id}){price_str}",
        f"State:     {state_dot(state)}",
        f"Activated: {activated}",
        f"Renews in: {_days_to(next_renewal, now=now)}",
        "",
        "── Bundle " + "─" * 50,
        *rows,
    ]

    # VAS history (if any)
    history = sub.get("vasHistory") or sub.get("topUps") or []
    if history:
        lines.append("")
        lines.extend(_vas_history_table(history))

    # eSIM block — integrated, not floating below the frame.
    if esim:
        lines.append("")
        lines.append("── eSIM " + "─" * 52)
        from ._box import format_iccid

        lines.append(f"  ICCID:    {format_iccid(esim.get('iccid', '—'))}")
        if esim.get("imsi"):
            lines.append(f"  IMSI:     {esim['imsi']}")
        code = esim.get("activationCode") or ""
        if code:
            lines.append(f"  LPA:      {code[:54]}")

    framer = double_box if state.lower() == "blocked" else box
    return framer(lines, title=f"Subscription {sub_id}", width=78)
