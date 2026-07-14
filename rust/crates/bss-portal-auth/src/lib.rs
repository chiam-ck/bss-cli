//! bss-portal-auth — email-based portal identity for the self-serve portal
//! (v0.8+). Rust port of `packages/bss-portal-auth`.
//!
//! **This sub-slice (P6a) ports the pure security foundation:** token primitives
//! (HMAC-pepper hashing + timing-safe verify, golden-vector-pinned against the
//! oracle), env config, the startup pepper validator, and the public
//! dataclasses. The **DB-backed service layer** (session lifecycle, step-up,
//! email-change, pending-action stash, rate limiter, per-write audit) and the
//! **email adapters + HTML renderers** land in later P6a sub-slices — they carry
//! the `portal_auth` schema store and the branding-aware email templates, so
//! they land with the portal that first drives them.
//!
//! Doctrine: OTP/magic-link/step-up tokens are stored as HMAC-SHA-256 hex keyed
//! by `BSS_PORTAL_TOKEN_PEPPER` (never logged, ≥32 chars, startup-validated),
//! compared timing-safe. Cookie carries a session id only.
#![forbid(unsafe_code)]

pub mod audit;
pub mod config;
pub mod email;
pub mod service;
pub mod startup;
pub mod tokens;
pub mod types;

pub use audit::{record_portal_action, PortalActionRecord};
pub use config::Settings;
pub use email::{
    resolve_provider_name, select_adapter, EmailAdapter, LoggingEmailAdapter, NoopEmailAdapter,
};
pub use service::{
    current_session, link_to_customer, revoke_session, rotate_if_due, start_email_login,
    verify_email_login, LinkError, LoginError, VerifyOutcome,
};
pub use startup::validate_pepper_present;
pub use tokens::{
    generate_magic_link_token, generate_otp, generate_session_id, generate_step_up_grant,
    hash_token, verify_token, PepperMissing, OTP_LENGTH,
};
pub use types::{
    IdentityView, LoginChallenge, LoginFailed, RateLimitExceeded, SessionView, StepUpChallenge,
    StepUpFailed, StepUpToken,
};
