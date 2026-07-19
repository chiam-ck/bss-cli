"""bss-clients factory for the self-serve portal (v0.9+, extended in v0.10).

The portal is a customer-facing surface; v0.9 gives it its own
identity at the BSS perimeter via ``BSS_PORTAL_SELF_SERVE_API_TOKEN``. Outbound
calls from this factory carry that token, so receiving services
resolve ``service_identity = "portal_self_serve"`` from token
validation. Audit / log / OTel see writes initiated through the
portal as distinct from orchestrator/CSR/scenario traffic.

Backwards compat: if ``BSS_PORTAL_SELF_SERVE_API_TOKEN`` is unset (rolling out
the named-token model staged across environments), the provider
falls back to ``BSS_API_TOKEN``. Receiving services then resolve
``service_identity = "default"`` for those calls. Logging surfaces
this with a one-time warning so operators notice and provision the
named token.

Doctrine note (V0_10_0.md + V0_11_0.md):

* **(v0.10+ / post-login self-serve)** Routes behind
  ``requires_linked_customer`` write directly through this factory.
  The customer principal is bound from ``request.state.customer_id``;
  per-resource ownership policies and step-up auth gate sensitive
  writes; one route = one write call.
* **(v0.11+ / signup)** The signup funnel joins the direct-write side.
  Steps live in ``routes/signup.py`` (``POST /signup`` →
  ``/signup/step/{kyc,cof,order,poll}``); each step is one write or
  zero. The chat surface (when it lands) is the only route that
  routes writes through the orchestrator.

``make doctrine-check`` enforces the boundary: the orchestrator
streaming entrypoint may only appear in chat routes; ``customer_id``
must come from ``request.state``, never form/query input.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from bss_clients import (
    CatalogClient,
    COMClient,
    CRMClient,
    InventoryClient,
    NamedTokenAuthProvider,
    PaymentClient,
    ProvisioningClient,
    SubscriptionClient,
)

from .config import settings

# v0.9 — the portal's perimeter identity. Receiving services resolve
# this from token validation against their TokenMap; this string is
# only used in caller-side log fields. Doctrine: distinct external
# surface = distinct named token.
PORTAL_IDENTITY = "portal_self_serve"
PORTAL_TOKEN_ENV = "BSS_PORTAL_SELF_SERVE_API_TOKEN"
FALLBACK_TOKEN_ENV = "BSS_API_TOKEN"


@dataclass(frozen=True)
class PortalClients:
    """Container for downstream clients the self-serve portal calls.

    v0.10 expands the bundle to cover every read/write the new
    post-login pages need: customer + subscription (dashboard, cancel,
    plan change), payment (COF + charge history), catalog (plan change
    list + VAS list), inventory (signup MSISDN picker), provisioning
    (eSIM service-id ownership lookup), com (signup orchestrator
    fallback). Doctrine: post-login routes write directly through
    these; signup + chat continue going through the orchestrator.
    """

    catalog: CatalogClient
    crm: CRMClient
    inventory: InventoryClient
    com: COMClient
    subscription: SubscriptionClient
    payment: PaymentClient
    provisioning: ProvisioningClient


@lru_cache(maxsize=1)
def get_clients() -> PortalClients:
    """Return the process-wide PortalClients bundle (constructed once).

    Wired with ``NamedTokenAuthProvider("portal_self_serve",
    "BSS_PORTAL_SELF_SERVE_API_TOKEN", fallback_env_var="BSS_API_TOKEN")`` — every
    outbound call carries the portal's named token (or falls back to
    the default token during rollout).
    """
    auth = NamedTokenAuthProvider(
        PORTAL_IDENTITY,
        PORTAL_TOKEN_ENV,
        fallback_env_var=FALLBACK_TOKEN_ENV,
    )
    return PortalClients(
        catalog=CatalogClient(base_url=settings.catalog_url, auth_provider=auth),
        crm=CRMClient(base_url=settings.crm_url, auth_provider=auth),
        # Inventory lives inside CRM (same base URL), per orchestrator factory.
        inventory=InventoryClient(base_url=settings.crm_url, auth_provider=auth),
        com=COMClient(base_url=settings.com_url, auth_provider=auth),
        subscription=SubscriptionClient(
            base_url=settings.subscription_url, auth_provider=auth
        ),
        payment=PaymentClient(base_url=settings.payment_url, auth_provider=auth),
        provisioning=ProvisioningClient(
            base_url=settings.provisioning_url, auth_provider=auth
        ),
    )


async def close_clients() -> None:
    """Close every client in the cached bundle. Called from portal shutdown."""
    if get_clients.cache_info().currsize == 0:
        return
    c = get_clients()
    for client in (
        c.catalog,
        c.crm,
        c.inventory,
        c.com,
        c.subscription,
        c.payment,
        c.provisioning,
    ):
        await client.close()
    get_clients.cache_clear()
