//! bss-webhooks — shared webhook substrate for BSS-CLI provider integrations
//! (v0.14). Rust port of `packages/bss-webhooks`.
//!
//! **This sub-slice (P6a) ports the pure, security-critical substrate:**
//! * [`signatures`] — HMAC verification across `svix` (Resend), `stripe`, and
//!   `didit_hmac`, golden-vector-pinned against the oracle. Built all-three
//!   upfront (the v0.14 doctrine: v0.16 must not be the first to touch shared
//!   HMAC under payment-scope pressure).
//! * [`redaction`] — `redact_provider_payload`, the second line of defence
//!   against persisting customer email / raw document numbers.
//! * [`idempotency`] — `idempotency_key`, the retry-safe outbound key format.
//!
//! **Deferred to the P6b portal consumer** (DB-backed, land-with-consumer):
//! `WebhookEventStore` (idempotent persist on `(provider, event_id)`) +
//! `ExternalCallStore` (forensic per-call log) over the `integrations` schema.
#![forbid(unsafe_code)]

pub mod idempotency;
pub mod redaction;
pub mod signatures;

pub use idempotency::idempotency_key;
pub use redaction::redact_provider_payload;
pub use signatures::{
    verify_signature, verify_signature_default, SignatureScheme, WebhookSignatureError,
};
