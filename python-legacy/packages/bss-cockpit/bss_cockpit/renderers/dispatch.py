"""Tool-result renderer dispatch — the SINGLE source of truth.

Both cockpit surfaces (the CLI REPL and the browser veneer) consume
the conversation store's ``tool``-role rows by passing the tool name
+ raw JSON result through ``render_tool_result``. The function returns
a deterministic ASCII string when a renderer is registered for the
tool, ``None`` otherwise.

Doctrine v0.19+: when a tool has no registered renderer the LLM is
instructed to surface the raw JSON verbatim and stop — never to fall
back to a markdown table. The browser veneer wraps any non-None
return value in ``<pre>`` so the visible output is byte-identical to
the REPL's. There is exactly ONE rendering rule for tool results;
this module is it.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .catalog import render_catalog, render_catalog_show, render_vas_list
from .customer import render_customer_360
from .esim import render_esim_activation
from .inventory import render_msisdn_count, render_msisdn_list
from .order import render_order
from .port_request import render_port_request_get, render_port_request_list
from .subscription import render_subscription


def _render_subscription_get(payload: dict) -> str:
    return render_subscription(payload)


def _render_subscription_list(payload: list) -> str:
    if not payload:
        return "(no subscriptions)"
    return "\n".join(render_subscription(s) for s in payload)


def _render_customer(payload: dict) -> str:
    """Render the customer 360, unpacking the optional ``_extras``
    block produced by ``customer.get`` (subscriptions / cases /
    interactions). Callers that hand us a bare TMF629 customer dict
    still render correctly — extras just default to empty.
    """
    extras = payload.get("_extras") or {}
    return render_customer_360(
        payload,
        subscriptions=extras.get("subscriptions") or [],
        cases=extras.get("cases") or [],
        interactions=extras.get("interactions") or [],
    )


def _render_customer_list(payload: list) -> str:
    if not payload:
        return "(no customers)"
    rows: list[str] = ["── Customers " + "─" * 50, ""]
    rows.append(f"  {'ID':<15}  {'Name':<24}  {'Status':<10}  Email")
    rows.append(f"  {'─' * 15}  {'─' * 24}  {'─' * 10}  {'─' * 30}")
    for c in payload[:25]:
        ind = c.get("individual") or {}
        name = " ".join(
            s for s in [ind.get("givenName"), ind.get("familyName")] if s
        ).strip() or c.get("name", "—")
        email = ""
        for cm in c.get("contactMedium") or []:
            if cm.get("mediumType") == "email":
                email = cm.get("value", "")
                break
        rows.append(
            f"  {c.get('id', '?'):<15}  {name[:24]:<24}  "
            f"{c.get('status', '?'):<10}  {email[:30]}"
        )
    if len(payload) > 25:
        rows.append(f"  (+ {len(payload) - 25} more)")
    return "\n".join(rows)


def _render_order(payload: dict) -> str:
    return render_order(payload)


def _render_order_list(payload: list) -> str:
    if not payload:
        return "(no orders)"
    rows: list[str] = ["── Orders " + "─" * 50, ""]
    rows.append(f"  {'ID':<14}  {'State':<14}  {'Customer':<16}  Placed")
    rows.append(f"  {'─' * 14}  {'─' * 14}  {'─' * 16}  {'─' * 19}")
    for o in payload[:25]:
        rows.append(
            f"  {o.get('id', '?'):<14}  {o.get('state', '?'):<14}  "
            f"{o.get('customerId', '—'):<16}  {(o.get('orderDate') or '')[:19]}"
        )
    if len(payload) > 25:
        rows.append(f"  (+ {len(payload) - 25} more)")
    return "\n".join(rows)


def _render_balance(payload: dict) -> str:
    fake = {
        "id": payload.get("subscriptionId", "—"),
        "state": payload.get("state", "?"),
        "balances": payload.get("balances") or [],
    }
    return render_subscription(fake)


def _render_catalog_list(payload: list) -> str:
    return render_catalog(payload)


def _render_catalog_show(payload: dict) -> str:
    return render_catalog_show(payload)


def _render_vas_list(payload: list) -> str:
    return render_vas_list(payload)


def _render_esim(payload: dict) -> str:
    return render_esim_activation(payload)


RENDERER_DISPATCH: dict[str, Callable[[Any], str]] = {
    # Single-entity get
    "subscription.get": _render_subscription_get,
    "customer.get": _render_customer,
    "customer.find_by_msisdn": _render_customer,
    "order.get": _render_order,
    "catalog.get_offering": _render_catalog_show,
    "inventory.esim.get_activation": _render_esim,
    "subscription.get_esim_activation": _render_esim,
    # Lists
    "subscription.list_for_customer": _render_subscription_list,
    "customer.list": _render_customer_list,
    "order.list": _render_order_list,
    "catalog.list_offerings": _render_catalog_list,
    "catalog.list_active_offerings": _render_catalog_list,
    "catalog.list_vas": _render_vas_list,
    "inventory.msisdn.list_available": render_msisdn_list,
    "inventory.msisdn.count": render_msisdn_count,
    "port_request.list": render_port_request_list,
    "port_request.get": render_port_request_get,
    # Balance
    "subscription.get_balance": _render_balance,
}


def render_tool_result(tool_name: str, raw_result: str) -> str | None:
    """Render a tool's stringified JSON result to deterministic ASCII.

    Returns ``None`` when no renderer is registered for ``tool_name``,
    when the raw_result isn't valid JSON, when the payload is empty,
    or when a registered renderer raises. The "best-effort, never
    break the surface" contract: a returning ``None`` lets the caller
    fall back to surfacing the raw JSON verbatim — never a markdown
    table.
    """
    renderer = RENDERER_DISPATCH.get(tool_name)
    if renderer is None:
        return None
    try:
        payload = json.loads(raw_result)
    except (ValueError, TypeError):
        return None
    if not payload:
        return None
    try:
        return renderer(payload)
    except Exception:  # noqa: BLE001
        return None
