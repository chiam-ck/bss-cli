"""Service Order + Service Inventory tools (TMF641 + TMF638)."""

from __future__ import annotations

from typing import Any

from ..clients import get_clients
from ..types import OrderId, ServiceId, ServiceOrderId, SubscriptionId
from ._registry import register


@register("service_order.get")
async def service_order_get(service_order_id: ServiceOrderId) -> dict[str, Any]:
    """Read a service order by ID.

    Args:
        service_order_id: Service Order ID with the SO- prefix (opaque suffix).

    Returns:
        Service order dict with items + state.

    Raises:
        NotFound: unknown service order.
    """
    return await get_clients().som.get_service_order(service_order_id)


@register("service_order.list_for_order")
async def service_order_list_for_order(
    commercial_order_id: OrderId,
) -> list[dict[str, Any]]:
    """List service orders decomposed from a commercial order. In v0.1 there's
    always exactly one SO per COM order.

    Args:
        commercial_order_id: Commercial Order ID with the ORD- prefix (opaque suffix).

    Returns:
        List of service order dicts.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().som.list_for_order(commercial_order_id)


@register("service.get")
async def service_get(service_id: ServiceId) -> dict[str, Any]:
    """Get a service with its state history and characteristics (MSISDN, ICCID).

    Args:
        service_id: Service ID with the SVC- prefix (opaque suffix).

    Returns:
        Service dict ``{id, serviceType, name, state, characteristics, parentServiceId?}``.

    Raises:
        NotFound: unknown service.
    """
    return await get_clients().som.get_service(service_id)


@register("service.list_for_subscription")
async def service_list_for_subscription(
    subscription_id: SubscriptionId,
) -> list[dict[str, Any]]:
    """List the CFS + RFS tree belonging to a subscription.

    Args:
        subscription_id: Subscription ID with the SUB- prefix (opaque suffix).

    Returns:
        Flat list of service dicts (mix of CFS and RFS). The CFS has
        ``parentServiceId = None``; each RFS points up to the CFS.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().som.list_services_for_subscription(subscription_id)
