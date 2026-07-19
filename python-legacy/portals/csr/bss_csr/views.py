"""Shared view helpers for the cockpit CRM screens (v1.6).

The BSS surfaces mix TMF camelCase payloads (customer, ticket,
subscription, order, payment) with internal snake_case DTOs (case,
port request). ``field()`` reads both spellings so route handlers and
templates never care which family a payload came from — the same
leniency the ASCII renderers in bss-cockpit apply.

Everything here is read-side presentation logic. No client calls, no
writes — keep it that way so the route modules stay the only place
that talks to services.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_CAMEL_RE = re.compile(r"_([a-z])")


def _camel(name: str) -> str:
    return _CAMEL_RE.sub(lambda m: m.group(1).upper(), name)


def field(d: dict[str, Any] | None, *names: str, default: Any = "") -> Any:
    """First non-empty value among ``names`` (each tried as given AND
    camelCased, so callers can write snake_case once)."""
    if not d:
        return default
    for n in names:
        for key in (n, _camel(n)):
            v = d.get(key)
            if v not in (None, ""):
                return v
    return default


def fmt_dt(value: Any) -> str:
    """Compact ``YYYY-MM-DD HH:MM`` for ISO strings/datetimes; '—' when empty."""
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    s = str(value)
    if "T" in s:
        return s[:16].replace("T", " ")
    return s[:16]


def state_tone(state: Any) -> str:
    """Map an entity state to a badge tone (ok / warn / err / muted)."""
    s = str(state or "").lower()
    if s in {
        "active", "completed", "resolved", "verified", "approved",
        "succeeded", "available", "delivered", "sellable",
    }:
        return "ok"
    if s in {
        "blocked", "failed", "stuck", "cancelled", "canceled", "declined",
        "terminated", "rejected", "exhausted", "errored", "ported_out",
        "suspended",
    }:
        return "err"
    if s in {
        "open", "in_progress", "pending", "pending_customer",
        "pending_activation", "acknowledged", "submitted", "processing",
        "reserved", "awaiting_payment", "draft", "requested",
    }:
        return "warn"
    return "muted"


def customer_name(c: dict[str, Any] | None) -> str:
    if not c:
        return "—"
    individual = c.get("individual") or {}
    name = " ".join(
        s for s in [individual.get("givenName"), individual.get("familyName")] if s
    ).strip()
    return name or c.get("name", "—") or "—"


def flatten_customer(c: dict[str, Any]) -> dict[str, Any]:
    """Card/table row view of a TMF629 customer payload."""
    email = ""
    msisdn = ""
    for cm in c.get("contactMedium") or []:
        ch = cm.get("characteristic") or {}
        if cm.get("mediumType") == "email" and not email:
            email = cm.get("value", "") or ch.get("emailAddress", "")
        if cm.get("mediumType") == "mobile" and not msisdn:
            msisdn = cm.get("value", "") or ch.get("phoneNumber", "")
    return {
        "id": c.get("id", "?"),
        "name": customer_name(c),
        "status": field(c, "status", default="?"),
        "kyc_status": field(c, "kyc_status", default="?"),
        "email": email,
        "msisdn": msisdn,
        "created_at": fmt_dt(field(c, "created_at", "since", default="")),
    }


def balance_rows(balances: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize bundle balances for the progress-bar partial.

    Accepts both the live subscription payload shape
    (``allowanceType``/``total``/``consumed``/``remaining``, ``-1`` total
    = unlimited) and the older renderer-test shape (``type``/``used``).
    """
    rows: list[dict[str, Any]] = []
    for b in balances or []:
        label = str(field(b, "allowance_type", "type", default="?"))
        total = b.get("total")
        used = b.get("consumed", b.get("used"))
        if used is None and b.get("remaining") is not None and total not in (None, -1):
            used = float(total) - float(b["remaining"])
        used_f = float(used or 0)
        unlimited = total in (None, -1, "unlimited")
        total_f = None if unlimited else float(total)
        pct = 0 if unlimited or not total_f else min(100, int(round(used_f / total_f * 100)))
        rows.append(
            {
                "label": label.replace("_", " "),
                "used": used_f,
                "total": total_f,
                "unit": str(b.get("unit", "")).upper(),
                "pct": pct,
                "unlimited": unlimited,
                "exhausted": (not unlimited) and total_f is not None and used_f >= total_f,
            }
        )
    return rows


def offering_price(o: dict[str, Any] | None) -> str:
    """``SGD 22`` style price string from a TMF620 offering payload."""
    if not o:
        return "—"
    pops = o.get("productOfferingPrice") or []
    if pops:
        price = (pops[0].get("price") or {}).get("taxIncludedAmount") or {}
        value = price.get("value")
        unit = price.get("unit", "SGD")
        if value is not None:
            return f"{unit} {value:g}" if isinstance(value, (int, float)) else f"{unit} {value}"
    flat = o.get("price") or o.get("monthlyPrice")
    return f"SGD {flat}" if flat is not None else "—"


def offering_allowance(o: dict[str, Any], kind: str) -> str:
    """Human string for one allowance bucket (data/voice/sms/data_roaming)."""
    for a in o.get("bundleAllowance") or o.get("allowances") or []:
        atype = a.get("allowanceType") or a.get("type")
        if atype != kind and not (kind == "voice" and atype == "voice_minutes"):
            continue
        qty = a.get("quantity") if "quantity" in a else a.get("total")
        unit = a.get("unit", "")
        if qty in (None, "unlimited") or qty == -1:
            return "unlimited"
        if unit == "mb" and isinstance(qty, (int, float)) and qty >= 1024:
            return f"{qty / 1024:g} GB"
        if unit in ("min", "minutes"):
            return f"{qty} min"
        if unit in ("sms", "count"):
            return f"{qty} sms"
        return f"{qty} {unit}".strip()
    return "—"


def flatten_order(o: dict[str, Any]) -> dict[str, Any]:
    items = o.get("items") or []
    return {
        "id": o.get("id", "?"),
        "customer_id": field(o, "customer_id", default="—"),
        "offering_id": field(items[0], "offering_id", default="—") if items else "—",
        "state": field(o, "state", default="?"),
        "order_date": fmt_dt(field(o, "order_date", "created_at", default="")),
        "completed_date": fmt_dt(field(o, "completed_date", default="")),
    }


def flatten_case(c: dict[str, Any]) -> dict[str, Any]:
    ticket_ids = c.get("ticket_ids") or c.get("ticketIds") or []
    tickets = c.get("tickets") or []
    return {
        "id": c.get("id", "?"),
        "customer_id": field(c, "customer_id", default="—"),
        "subject": c.get("subject") or "(no subject)",
        "state": field(c, "state", default="?"),
        "priority": field(c, "priority", default="—"),
        "category": field(c, "category", default="—"),
        "opened_at": fmt_dt(field(c, "opened_at", "created_at", default="")),
        "ticket_count": len(ticket_ids) or len(tickets),
    }


def flatten_ticket(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": t.get("id", "?"),
        "type": field(t, "ticket_type", "type", default="—"),
        "subject": t.get("subject") or "(no subject)",
        "state": field(t, "state", default="?"),
        "priority": field(t, "priority", default=""),
        "agent_id": field(t, "assigned_to_agent_id", "agent_id", default=""),
        "customer_id": field(t, "customer_id", default=""),
        "case_id": field(t, "case_id", default=""),
        "opened_at": fmt_dt(field(t, "opened_at", default="")),
    }
