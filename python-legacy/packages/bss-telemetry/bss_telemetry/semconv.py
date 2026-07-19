"""BSS-CLI-specific span attribute keys.

PII discipline: every key here must be a prefixed ID string or a
status enum. Never raw email, NRIC, card number, full ICCID/Ki, or
personal name. The CI grep guard in v0.2 enforces no raw PII keys
appear in business code's ``set_attribute`` calls.
"""

# Customer / account identifiers
BSS_CUSTOMER_ID = "bss.customer_id"
BSS_TENANT_ID = "bss.tenant_id"
BSS_KYC_STATUS = "bss.kyc_status"

# Order / Service Order identifiers
BSS_ORDER_ID = "bss.order_id"
BSS_SERVICE_ORDER_ID = "bss.service_order_id"
BSS_OFFERING_ID = "bss.offering_id"

# Subscription / VAS identifiers
BSS_SUBSCRIPTION_ID = "bss.subscription_id"
BSS_VAS_OFFERING_ID = "bss.vas_offering_id"
BSS_SUBSCRIPTION_STATE = "bss.subscription_state"

# Service / Resource identifiers — last4 only, never full
BSS_SERVICE_ID = "bss.service_id"
BSS_MSISDN_LAST4 = "bss.msisdn.last4"
BSS_ICCID_LAST4 = "bss.iccid.last4"

# Caller context (also propagated via X-BSS-* headers; useful on root spans)
BSS_ACTOR = "bss.actor"
BSS_CHANNEL = "bss.channel"
# v0.9 — perimeter-resolved identity. Comes from validated token, not
# a header. Lets `bss trace` filter spans by initiating surface
# (default / portal_self_serve / partner_<name>).
BSS_SERVICE_IDENTITY = "bss.service.identity"
