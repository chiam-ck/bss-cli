"""Flatten the TMF productOffering payload into plain dicts for templates.

The catalog returns TMF-shaped JSON (``productOfferingPrice[0].price.
taxIncludedAmount.value``, etc.). Templates want simple keys — ``price``,
``data``, ``voice``, ``sms``. This module is the translation seam; it
mirrors the logic in ``cli/bss_cli/renderers/catalog.py`` but emits
dicts instead of ASCII.
"""

from __future__ import annotations

from typing import Any


def _price_value(p: dict[str, Any]) -> float:
    """Sort key — recurring price ascending. Offerings without a numeric
    price sink to the end so the catalog can grow new shapes without
    breaking ordering."""
    pops = p.get("productOfferingPrice") or []
    if pops:
        amount = (pops[0].get("price") or {}).get("taxIncludedAmount") or {}
        v = amount.get("value")
        if isinstance(v, (int, float)):
            return float(v)
    return float("inf")


def _is_sellable_plan(o: dict[str, Any]) -> bool:
    """Active, sellable, bundle (i.e. a plan offering, not VAS)."""
    return (
        o.get("isSellable", True)
        and (o.get("lifecycleStatus") or "active") == "active"
        and o.get("isBundle", True)
    )


def _allowance_str(allowances: list[dict[str, Any]], kind: str) -> str:
    for a in allowances or []:
        atype = a.get("allowanceType") or a.get("type")
        if atype != kind:
            continue
        qty = a.get("quantity") if "quantity" in a else a.get("total")
        unit = a.get("unit", "")
        if qty in (None, "unlimited") or qty == -1:
            return "unlimited"
        if unit == "mb" and isinstance(qty, (int, float)) and qty >= 1024:
            return f"{qty / 1024:g} GB"
        return f"{qty} {unit}".strip()
    return "—"


def _price_str(p: dict[str, Any]) -> str:
    pops = p.get("productOfferingPrice") or []
    if pops:
        amount = (pops[0].get("price") or {}).get("taxIncludedAmount") or {}
        value = amount.get("value")
        if value is not None:
            return f"{value:g}"
    flat = p.get("price") or p.get("monthlyPrice")
    return str(flat) if flat is not None else "?"


def flatten_offerings(offerings: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return template-shaped dicts for every active sellable plan,
    sorted cheapest-first. New offerings added via
    ``bss admin catalog add-offering`` appear automatically — no source
    edit required (#36)."""
    plans: list[dict[str, str]] = []
    for p in sorted(
        (o for o in offerings if _is_sellable_plan(o)),
        key=_price_value,
    ):
        allowances = p.get("bundleAllowance") or p.get("allowances") or []
        voice = _allowance_str(allowances, "voice")
        if voice == "—":
            voice = _allowance_str(allowances, "voice_minutes")
        # v0.17 — additive roaming bucket. PLAN_S has 0 mb so we
        # surface ``None`` (template suppresses the row); PLAN_M/L
        # show their bundled MB. Mirrors the dashboard's line_card
        # filter so a stranded "Roaming 0/0" row never appears.
        roaming = _allowance_str(allowances, "data_roaming")
        plans.append(
            {
                "id": p["id"],
                "name": p.get("name", p["id"]),
                "price": _price_str(p),
                "data": _allowance_str(allowances, "data"),
                "voice": voice,
                "sms": _allowance_str(allowances, "sms"),
                "roaming": roaming if roaming not in ("—", "0 mb") else None,
            }
        )
    return plans


def find_plan(flattened: list[dict[str, str]], plan_id: str) -> dict[str, str] | None:
    for p in flattened:
        if p["id"] == plan_id:
            return p
    return None
