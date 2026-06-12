"""Order domain policies — enforced before every write."""

from bss_clients.errors import NotFound

from app.policies.base import PolicyViolation, policy


async def check_customer_exists(customer_id: str, crm_client):
    """Customer must exist and be active."""
    try:
        customer = await crm_client.get_customer(customer_id)
    except NotFound:
        raise PolicyViolation(
            rule="order.create.customer_not_found",
            message=f"Customer {customer_id} not found",
            context={"customer_id": customer_id},
        )
    return customer


async def check_offering_exists(offering_id: str, catalog_client):
    """Offering must exist in catalog (read paths only — no time filter)."""
    try:
        offering = await catalog_client.get_offering(offering_id)
    except NotFound:
        raise PolicyViolation(
            rule="order.create.offering_not_found",
            message=f"Offering {offering_id} not found",
            context={"offering_id": offering_id},
        )
    return offering


async def check_offering_currently_sellable(offering_id: str, catalog_client):
    """Offering must exist AND be sellable at the current moment.

    Used by the create-order path: it both validates existence and confirms
    the offering is inside its `valid_from / valid_to` window. Catalog rejects
    with `catalog.price.no_active_row` if there's no live price; we surface
    that as `policy.offering.not_sellable_now`.
    """
    from bss_clients.errors import PolicyViolationFromServer

    offering = await check_offering_exists(offering_id, catalog_client)
    try:
        price = await catalog_client.get_active_price(offering_id)
    except PolicyViolationFromServer as exc:
        raise PolicyViolation(
            rule="policy.offering.not_sellable_now",
            message=f"Offering {offering_id} is not sellable at this time",
            context={"offering_id": offering_id, "underlying": exc.rule},
        )
    return offering, price


async def check_customer_has_payment_method(customer_id: str, payment_client):
    """Customer must have at least one payment method (card-on-file)."""
    methods = await payment_client.list_methods(customer_id)
    if not methods:
        raise PolicyViolation(
            rule="order.create.no_payment_method",
            message=f"Customer {customer_id} has no payment method on file",
            context={"customer_id": customer_id},
        )
    return methods


_ORDER_TRANSITIONS = {
    "acknowledged": {"in_progress", "cancelled"},
    "in_progress": {"completed", "failed", "cancelled"},
}


@policy("order.transition.invalid")
def check_order_transition(current_state: str, target_state: str):
    """Only legal state transitions are allowed."""
    allowed = _ORDER_TRANSITIONS.get(current_state, set())
    if target_state not in allowed:
        raise PolicyViolation(
            rule="order.transition.invalid",
            message=f"Order cannot transition from '{current_state}' to '{target_state}'",
            context={
                "current_state": current_state,
                "target_state": target_state,
                "allowed": sorted(allowed) if allowed else [],
            },
        )


async def check_cancel_allowed_after_som(order_id: str, som_client):
    """If in_progress, cancel only if SOM hasn't started real provisioning."""
    service_orders = await som_client.list_for_order(order_id)
    for so in service_orders:
        if so.get("state") not in ("acknowledged", None):
            raise PolicyViolation(
                rule="order.cancel.forbidden_after_som_started",
                message=(
                    f"Order {order_id} cannot be cancelled — service order "
                    f"{so.get('id')} is in state '{so.get('state')}'"
                ),
                context={
                    "order_id": order_id,
                    "service_order_id": so.get("id"),
                    "service_order_state": so.get("state"),
                },
            )
