//! bss-db — shared database plumbing + the `PolicyViolation` error.
//!
//! Rust port of the services' `policies/base` (the `PolicyViolation` type and its
//! wire serialization) plus the shared SQLAlchemy engine config. The
//! `PolicyViolation` wire shape is a frozen contract the LLM depends on.
#![forbid(unsafe_code)]

pub mod migrate;
mod policy;
mod pool;

/// Default tenant on every row until multi-tenant work (Phase 12-retired).
pub const DEFAULT_TENANT: &str = "DEFAULT";

pub use policy::{PolicyViolation, POLICY_VIOLATION_CODE};
pub use pool::{connect, POOL_MAX_OVERFLOW, POOL_SIZE};

/// Re-export the pool type so services don't depend on sqlx's path directly.
pub use sqlx::PgPool;
