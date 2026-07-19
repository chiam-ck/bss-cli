//! Pre-baked KYC attestation constants for the signup chain. Port of
//! `bss_self_serve.prompts`.
//!
//! Passed to `crm.attest_kyc` when the portal runs with
//! `BSS_PORTAL_KYC_PROVIDER=prebaked` (dev default; production runs `didit`).
//! The per-customer signature template is formatted with the customer's email
//! so the `document_hash_unique_per_tenant` policy doesn't reject a duplicate.

/// The displayed prebaked attestation id. Stable across all signups.
pub const KYC_PREBAKED_ATTESTATION_ID: &str = "KYC-PREBAKED-001";

/// Per-customer signature template — format with the customer's email.
pub const KYC_PREBAKED_SIGNATURE_TEMPLATE: &str = "prebaked-simulated-v1::{email}";

/// Render [`KYC_PREBAKED_SIGNATURE_TEMPLATE`] for a given email.
pub fn prebaked_signature(email: &str) -> String {
    KYC_PREBAKED_SIGNATURE_TEMPLATE.replace("{email}", email)
}
