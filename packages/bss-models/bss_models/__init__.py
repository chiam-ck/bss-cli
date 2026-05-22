"""BSS-CLI shared models package.

Imports all domain models so that ``Base.metadata`` contains every table
when Alembic (or any other tool) loads this package. Also exports the
single source-of-truth ``BSS_RELEASE`` version string used by every
surface (REPL banner, self-serve portal brand-tag, CSR cockpit health,
service ``/health`` versions). One bump per release; doctrine.
"""

# Single source of truth for the platform release version. Every surface
# (REPL, CSR cockpit, self-serve portal, service /health) imports this
# so a release bump is one line. Bump on every release tag.
BSS_RELEASE = "1.1.3"

from .base import Base, TenantMixin, TimestampMixin

# CRM (12 tables)
from .crm import (
    Agent,
    Case,
    CaseNote,
    ContactMedium,
    Customer,
    CustomerIdentity,
    Individual,
    Interaction,
    Party,
    PortRequest,
    SlaPolicy,
    Ticket,
    TicketStateHistory,
)

# Catalog (7 tables)
from .catalog import (
    BundleAllowance,
    ProductOffering,
    ProductOfferingPrice,
    ProductSpecification,
    ProductToServiceMapping,
    Promotion,
    PromotionEligibility,
    ServiceSpecification,
    VasOffering,
)

# Inventory (2 tables)
from .inventory import EsimProfile, MsisdnPool

# Payment (3 tables; v0.16 added PaymentCustomer)
from .payment import PaymentAttempt, PaymentCustomer, PaymentMethod

# Order Management (3 tables)
from .order_mgmt import OrderItem, OrderStateHistory, ProductOrder

# Service Inventory (4 tables)
from .service_inventory import (
    Service,
    ServiceOrder,
    ServiceOrderItem,
    ServiceStateHistory,
)

# Provisioning (2 tables)
from .provisioning import FaultInjection, ProvisioningTask

# Subscription (4 tables)
from .subscription import (
    BundleBalance,
    Subscription,
    SubscriptionStateHistory,
    VasPurchase,
)

# Mediation (1 table)
from .mediation import UsageEvent

# Billing (2 tables)
from .billing import BillingAccount, CustomerBill

# Audit (3 tables — v0.12 added ChatUsage + ChatTranscript)
from .audit import ChatTranscript, ChatUsage, DomainEvent

# Integrations (3 tables) — v0.14 + v0.15
from .integrations import ExternalCall, KycWebhookCorroboration, WebhookEvent

# Portal Auth (4 tables) — v0.8
from .portal_auth import (
    EmailChangePending,
    Identity,
    LoginAttempt,
    LoginToken,
    PortalAction,
    Session,
    StepUpPendingAction,
)

# Promo discount math — pure helpers shared across services (v1.1)
from .discount import apply_discount, discount_label

__all__ = [
    "BSS_RELEASE",
    "Base",
    "apply_discount",
    "discount_label",
    "TenantMixin",
    "TimestampMixin",
    # CRM
    "Party",
    "Individual",
    "ContactMedium",
    "Customer",
    "CustomerIdentity",
    "Interaction",
    "Agent",
    "Case",
    "CaseNote",
    "Ticket",
    "TicketStateHistory",
    "SlaPolicy",
    "PortRequest",
    # Catalog
    "ProductSpecification",
    "ProductOffering",
    "ProductOfferingPrice",
    "Promotion",
    "PromotionEligibility",
    "BundleAllowance",
    "VasOffering",
    "ServiceSpecification",
    "ProductToServiceMapping",
    # Inventory
    "MsisdnPool",
    "EsimProfile",
    # Payment
    "PaymentMethod",
    "PaymentAttempt",
    "PaymentCustomer",
    # Order Management
    "ProductOrder",
    "OrderItem",
    "OrderStateHistory",
    # Service Inventory
    "ServiceOrder",
    "ServiceOrderItem",
    "Service",
    "ServiceStateHistory",
    # Provisioning
    "ProvisioningTask",
    "FaultInjection",
    # Subscription
    "Subscription",
    "BundleBalance",
    "VasPurchase",
    "SubscriptionStateHistory",
    # Mediation
    "UsageEvent",
    # Billing
    "BillingAccount",
    "CustomerBill",
    # Audit
    "DomainEvent",
    "ChatUsage",
    "ChatTranscript",
    # Integrations (v0.14 + v0.15)
    "ExternalCall",
    "WebhookEvent",
    "KycWebhookCorroboration",
    # Portal Auth (v0.8 + v0.10 PortalAction + v0.10 EmailChangePending)
    "Identity",
    "LoginToken",
    "Session",
    "LoginAttempt",
    "PortalAction",
    "EmailChangePending",
    "StepUpPendingAction",
]
