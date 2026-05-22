"""bss-clients — shared HTTP client package for service-to-service calls."""

from .admin import AdminClient
from .audit import AuditClient
from .auth import (
    AuthProvider,
    BearerAuthProvider,
    NamedTokenAuthProvider,
    NoAuthProvider,
    TokenAuthProvider,
)
from .base import (
    BSSClient,
    reset_service_identity_token,
    set_context,
    set_service_identity_token,
)
from .catalog import CatalogClient
from .com import COMClient
from .crm import CRMClient
from .errors import ClientError, NotFound, PolicyViolationFromServer, ServerError, Timeout
from .inventory import InventoryClient
from .loyalty import LoyaltyClient
from .mediation import MediationClient
from .payment import PaymentClient
from .provisioning import ProvisioningClient
from .som import SOMClient
from .subscription import SubscriptionClient

__all__ = [
    "AdminClient",
    "AuditClient",
    "AuthProvider",
    "BSSClient",
    "BearerAuthProvider",
    "COMClient",
    "CRMClient",
    "CatalogClient",
    "ClientError",
    "InventoryClient",
    "LoyaltyClient",
    "MediationClient",
    "NamedTokenAuthProvider",
    "NoAuthProvider",
    "NotFound",
    "PaymentClient",
    "PolicyViolationFromServer",
    "ProvisioningClient",
    "SOMClient",
    "ServerError",
    "SubscriptionClient",
    "Timeout",
    "TokenAuthProvider",
    "reset_service_identity_token",
    "set_context",
    "set_service_identity_token",
]
