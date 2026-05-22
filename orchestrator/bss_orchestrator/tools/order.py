"""Commercial Order tools — TMF622 productOrder surface."""

from __future__ import annotations

from typing import Any

from ..clients import get_clients
from ..types import CustomerId, Msisdn, OrderId, OrderState, ProductOfferingId
from ._registry import register


@register("order.create")
async def order_create(
    customer_id: CustomerId,
    offering_id: ProductOfferingId,
    msisdn_preference: Msisdn | None = None,
    notes: str | None = None,
    discount_code: str | None = None,
) -> dict[str, Any]:
    """Create AND submit a commercial order. The customer must have an active
    card-on-file (``payment.add_card``) and a valid KYC attestation before
    this call will succeed.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).
        offering_id: An active sellable plan ID. Get the current set
            from ``catalog.list_active_offerings`` — operators may add
            new plans at runtime; never hardcode the v1.0 trio.
        msisdn_preference: Optional preferred MSISDN. If unset or unavailable,
            SOM auto-picks one from the pool.
        notes: Optional free text stored on the order.
        discount_code: Optional promo code (v1.1). Validated + composed by
            catalog; an invalid code never blocks the order (it just proceeds
            at full price). When omitted, any offer pre-assigned to the
            customer auto-applies. Consumed at activation, not at create.

    Returns:
        Order dict ``{id, customerId, items, state, orderDate}``. Expect
        ``state="acknowledged"`` immediately; use ``order.wait_until`` to
        wait for ``completed``.

    Raises:
        PolicyViolationFromServer:
            - ``order.create.requires_cof``: customer has no active card.
              Fix with ``payment.add_card`` then retry.
            - ``order.create.requires_verified_customer``: KYC not verified.
              Fix with ``customer.attest_kyc`` (channel has the token) then retry.
            - ``order.create.offering_not_sellable``: plan is inactive.
    """
    c = get_clients()
    order = await c.com.create_order(
        customer_id=customer_id,
        offering_id=offering_id,
        msisdn_preference=msisdn_preference,
        notes=notes,
        discount_code=discount_code,
    )
    return await c.com.submit_order(order["id"])


@register("order.get")
async def order_get(order_id: OrderId) -> dict[str, Any]:
    """Read an order with its items and current state.

    Args:
        order_id: Order ID with the ORD- prefix (opaque suffix).

    Returns:
        Order dict.

    Raises:
        NotFound: unknown order.
    """
    return await get_clients().com.get_order(order_id)


@register("order.list")
async def order_list(customer_id: CustomerId) -> list[dict[str, Any]]:
    """List orders for a customer, newest first.

    Args:
        customer_id: Customer ID with the CUST- prefix (opaque suffix).

    Returns:
        List of order dicts.

    Raises:
        (none expected — read tool)
    """
    return await get_clients().com.list_orders(customer_id)


@register("order.cancel")
async def order_cancel(order_id: OrderId) -> dict[str, Any]:
    """Cancel an order before SOM starts it. DESTRUCTIVE — gated by ``safety.py``.

    Args:
        order_id: Order ID with the ORD- prefix (opaque suffix).

    Returns:
        Updated order dict with ``state="cancelled"``.

    Raises:
        PolicyViolationFromServer:
            - ``order.cancel.som_already_started``: too late — orchestrate
              termination via ``subscription.terminate`` after completion.
    """
    return await get_clients().com.cancel_order(order_id)


@register("order.wait_until")
async def order_wait_until(
    order_id: OrderId,
    target_state: OrderState = "completed",
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Poll an order until it reaches ``target_state`` or times out. Returns
    early if it reaches a different terminal state (``failed``, ``cancelled``).

    Args:
        order_id: Order ID with the ORD- prefix (opaque suffix).
        target_state: The state to wait for (default ``completed``).
        timeout_s: Seconds to wait (default 30).

    Returns:
        The final order dict.

    Raises:
        Timeout: the order did not reach ``target_state`` within ``timeout_s``.
            If this happens, read ``order.get`` to see current state and
            ``service_order.list_for_order`` + ``provisioning.list_tasks`` to
            understand where things are stuck.
    """
    return await get_clients().com.wait_until(
        order_id, target_state=target_state, timeout_s=timeout_s
    )
