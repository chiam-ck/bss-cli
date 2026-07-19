"""bss-clients factory — single place where clients are constructed.

Tools and CLI commands call ``get_clients()`` rather than constructing clients
themselves. This ensures base URLs come from one config source and swapping
an endpoint (e.g. local vs docker-compose hostnames) is a one-line change.

Clients are lazily built once per process and cached in a ``Clients`` dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from bss_clients import (
    CatalogClient,
    COMClient,
    CRMClient,
    InventoryClient,
    MediationClient,
    PaymentClient,
    ProvisioningClient,
    SOMClient,
    SubscriptionClient,
    TokenAuthProvider,
)
from bss_middleware import api_token

from .config import settings


@dataclass(frozen=True)
class Clients:
    """Container for every downstream service client."""

    catalog: CatalogClient
    crm: CRMClient
    inventory: InventoryClient
    payment: PaymentClient
    com: COMClient
    som: SOMClient
    subscription: SubscriptionClient
    mediation: MediationClient
    provisioning: ProvisioningClient


@lru_cache(maxsize=1)
def get_clients() -> Clients:
    """Return the process-wide ``Clients`` bundle (constructed on first call).

    v0.3+: every client carries ``X-BSS-API-Token`` via TokenAuthProvider
    so the receiving services' BSSApiTokenMiddleware accepts the request.
    """
    auth = TokenAuthProvider(api_token())
    return Clients(
        catalog=CatalogClient(base_url=settings.catalog_url, auth_provider=auth),
        crm=CRMClient(base_url=settings.crm_url, auth_provider=auth),
        # Inventory lives inside CRM (port 8002) — same base URL.
        inventory=InventoryClient(base_url=settings.crm_url, auth_provider=auth),
        payment=PaymentClient(base_url=settings.payment_url, auth_provider=auth),
        com=COMClient(base_url=settings.com_url, auth_provider=auth),
        som=SOMClient(base_url=settings.som_url, auth_provider=auth),
        subscription=SubscriptionClient(base_url=settings.subscription_url, auth_provider=auth),
        mediation=MediationClient(base_url=settings.mediation_url, auth_provider=auth),
        provisioning=ProvisioningClient(base_url=settings.provisioning_url, auth_provider=auth),
    )


async def close_clients() -> None:
    """Close every client in the cached bundle (CLI shutdown hook)."""
    if get_clients.cache_info().currsize == 0:
        return
    c = get_clients()
    for client in (
        c.catalog,
        c.crm,
        c.inventory,
        c.payment,
        c.com,
        c.som,
        c.subscription,
        c.mediation,
        c.provisioning,
    ):
        await client.close()
    get_clients.cache_clear()
