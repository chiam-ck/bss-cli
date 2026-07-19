"""VAS purchase policies."""

from bss_clients import CatalogClient
from bss_clients.errors import NotFound

from app.policies.base import PolicyViolation, policy


@policy("subscription.vas_purchase.not_if_terminated")
def check_not_terminated(state: str) -> None:
    if state == "terminated":
        raise PolicyViolation(
            rule="subscription.vas_purchase.not_if_terminated",
            message="Cannot purchase VAS on terminated subscription",
            context={"state": state},
        )


@policy("subscription.vas_purchase.vas_offering_sellable")
async def check_vas_offering_sellable(
    vas_offering_id: str, catalog_client: CatalogClient
) -> dict:
    """VAS offering must exist."""
    try:
        vas = await catalog_client.get_vas(vas_offering_id)
    except NotFound:
        raise PolicyViolation(
            rule="subscription.vas_purchase.vas_offering_sellable",
            message=f"VAS offering {vas_offering_id} not found",
            context={"vas_offering_id": vas_offering_id},
        )
    return vas
