"""Subscription policies."""

from bss_clients import CRMClient, InventoryClient
from bss_clients.errors import NotFound

from app.policies.base import PolicyViolation, policy


@policy("subscription.create.requires_customer")
async def check_customer_exists(customer_id: str, crm_client: CRMClient) -> dict:
    """Customer must exist and be active or pending."""
    try:
        customer = await crm_client.get_customer(customer_id)
    except NotFound:
        raise PolicyViolation(
            rule="subscription.create.requires_customer",
            message=f"Customer {customer_id} not found",
            context={"customer_id": customer_id},
        )
    status = customer.get("status", "")
    if status not in ("active", "pending"):
        raise PolicyViolation(
            rule="subscription.create.requires_customer",
            message=f"Customer {customer_id} is {status}, must be active or pending",
            context={"customer_id": customer_id, "status": status},
        )
    return customer


@policy("subscription.create.msisdn_and_esim_reserved")
async def check_msisdn_and_esim_reserved(
    msisdn: str, iccid: str, inventory_client: InventoryClient
) -> None:
    """Both MSISDN and eSIM must be in reserved state."""
    try:
        msisdn_info = await inventory_client.get_msisdn(msisdn)
    except NotFound:
        raise PolicyViolation(
            rule="subscription.create.msisdn_and_esim_reserved",
            message=f"MSISDN {msisdn} not found",
            context={"msisdn": msisdn},
        )
    if msisdn_info.get("status") not in ("reserved", "assigned"):
        raise PolicyViolation(
            rule="subscription.create.msisdn_and_esim_reserved",
            message=f"MSISDN {msisdn} is {msisdn_info.get('status')}, must be reserved",
            context={"msisdn": msisdn, "status": msisdn_info.get("status")},
        )

    try:
        esim_info = await inventory_client.get_esim(iccid)
    except NotFound:
        raise PolicyViolation(
            rule="subscription.create.msisdn_and_esim_reserved",
            message=f"eSIM {iccid} not found",
            context={"iccid": iccid},
        )
    if esim_info.get("profileState", esim_info.get("profile_state")) not in ("reserved", "assigned"):
        raise PolicyViolation(
            rule="subscription.create.msisdn_and_esim_reserved",
            message=f"eSIM {iccid} is not reserved",
            context={"iccid": iccid},
        )


@policy("subscription.renew.only_if_active_or_blocked")
def check_renew_allowed(state: str) -> None:
    if state not in ("active", "blocked"):
        raise PolicyViolation(
            rule="subscription.renew.only_if_active_or_blocked",
            message=f"Cannot renew subscription in state '{state}'",
            context={"state": state},
        )
