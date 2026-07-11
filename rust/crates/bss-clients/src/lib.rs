//! bss-clients — the service-to-service HTTP base.
//!
//! Rust port of `packages/bss-clients`' base + auth. The 12 typed per-service
//! clients (CRMClient, CatalogClient, …) port lazily — each lands in the phase
//! that needs it (P1–P4), as thin wrappers over [`BssClient`].
//!
//! Context propagation is unified with bss-context: there is no `set_context`
//! (the Python contextvar setter) — the base reads [`bss_context::current`], the
//! task-local the server middleware installs. §2.1 of 02-TECH-MAPPING.md.
#![forbid(unsafe_code)]

mod auth;
mod base;
mod errors;

pub use auth::{
    AuthError, AuthProvider, BearerAuthProvider, NamedTokenAuthProvider, NoAuthProvider,
    TokenAuthProvider,
};
pub use base::{BssClient, DEFAULT_TIMEOUT};
pub use errors::ClientError;
