"""v0.17 — PortRequest tools (operator-only MNP).

Registered in the ``operator_cockpit`` profile only. Doctrine v0.17+:
MNP is operator-driven by spec (donor-carrier coordination, fraud
screen, regulatory clearance) — exposing these to a customer-side
profile would let the LLM act on numbers it has no business touching.
"""

from __future__ import annotations

from typing import Any

from ..clients import get_clients
from ..types import (
    IsoDatetime,
    Msisdn,
    PortDirection,
    PortRequestId,
    PortRequestState,
    SubscriptionId,
)
from ._registry import register


@register("port_request.list")
async def port_request_list(
    state: PortRequestState | None = None,
    direction: PortDirection | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List port requests (port-in + port-out).

    Args:
        state: Optional FSM filter — requested|validated|completed|rejected.
        direction: Optional filter — port_in|port_out.
        limit: Max rows (default 50).
        offset: Pagination offset.

    Returns:
        List of port request dicts.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().crm.list_port_requests(
        state=state, direction=direction, limit=limit, offset=offset
    )


@register("port_request.get")
async def port_request_get(
    port_request_id: PortRequestId,
) -> dict[str, Any]:
    """Fetch one port request by id (PORT-NNN).

    Args:
        port_request_id: PORT-NNN identifier.

    Returns:
        Port request dict.

    Raises:
        NotFound: unknown id.
    """
    return await get_clients().crm.get_port_request(port_request_id)


@register("port_request.create")
async def port_request_create(
    direction: PortDirection,
    donor_carrier: str,
    donor_msisdn: Msisdn,
    requested_port_date: IsoDatetime,
    target_subscription_id: SubscriptionId | None = None,
) -> dict[str, Any]:
    """Open a new port request.

    Args:
        direction: port_in (customer brings number to us) or
            port_out (customer takes number to a competitor).
        donor_carrier: Carrier the number is moving from.
        donor_msisdn: The MSISDN being ported.
        requested_port_date: ISO-8601 date (YYYY-MM-DD).
        target_subscription_id: Required for port_out; optional for
            port_in (the subscription may not exist yet at the
            point the port-in is logged).

    Returns:
        Newly-created port request dict.

    Raises:
        PolicyViolation: donor MSISDN already has an open port request,
            invalid direction, or port_out missing target_subscription_id.
    """
    return await get_clients().crm.create_port_request(
        direction=direction,
        donor_carrier=donor_carrier,
        donor_msisdn=donor_msisdn,
        requested_port_date=requested_port_date,
        target_subscription_id=target_subscription_id,
    )


@register("port_request.approve")
async def port_request_approve(
    port_request_id: PortRequestId,
) -> dict[str, Any]:
    """Approve a port request — completes the MNP flow.

    Port-in: donor MSISDN gets seeded into the pool (assigned to
    target_subscription_id if set, else available for normal
    signup). Port-out: the donor MSISDN flips to terminal
    ported_out and the target subscription is terminated.

    Args:
        port_request_id: PORT-NNN identifier.

    Returns:
        Updated port request dict (state=completed).

    Raises:
        PolicyViolation: invalid state for this transition.
    """
    return await get_clients().crm.approve_port_request(port_request_id)


@register("port_request.reject")
async def port_request_reject(
    port_request_id: PortRequestId, reason: str
) -> dict[str, Any]:
    """Reject a port request with a reason.

    Args:
        port_request_id: PORT-NNN identifier.
        reason: Required free-text reason (e.g. "donor carrier denied").

    Returns:
        Updated port request dict (state=rejected).

    Raises:
        PolicyViolation: missing reason or invalid state.
    """
    return await get_clients().crm.reject_port_request(
        port_request_id, reason=reason
    )
