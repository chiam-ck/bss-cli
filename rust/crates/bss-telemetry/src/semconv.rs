//! BSS-CLI span attribute keys — port of `bss_telemetry.semconv`.
//!
//! PII discipline: every key here is a prefixed ID string or a status enum —
//! never raw email, NRIC, card number, full ICCID/Ki, or personal name. IDs that
//! could be sensitive appear as `.last4` only. A doctrine grep guard keeps raw
//! PII keys out of `set_attribute` calls.

// Customer / account identifiers
pub const BSS_CUSTOMER_ID: &str = "bss.customer_id";
pub const BSS_TENANT_ID: &str = "bss.tenant_id";
pub const BSS_KYC_STATUS: &str = "bss.kyc_status";

// Order / Service Order identifiers
pub const BSS_ORDER_ID: &str = "bss.order_id";
pub const BSS_SERVICE_ORDER_ID: &str = "bss.service_order_id";
pub const BSS_OFFERING_ID: &str = "bss.offering_id";

// Subscription / VAS identifiers
pub const BSS_SUBSCRIPTION_ID: &str = "bss.subscription_id";
pub const BSS_VAS_OFFERING_ID: &str = "bss.vas_offering_id";
pub const BSS_SUBSCRIPTION_STATE: &str = "bss.subscription_state";

// Service / Resource identifiers — last4 only, never full
pub const BSS_SERVICE_ID: &str = "bss.service_id";
pub const BSS_MSISDN_LAST4: &str = "bss.msisdn.last4";
pub const BSS_ICCID_LAST4: &str = "bss.iccid.last4";

// Caller context
pub const BSS_ACTOR: &str = "bss.actor";
pub const BSS_CHANNEL: &str = "bss.channel";
/// v0.9 — perimeter-resolved identity (from validated token, not a header).
pub const BSS_SERVICE_IDENTITY: &str = "bss.service.identity";
