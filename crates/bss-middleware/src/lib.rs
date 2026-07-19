//! bss-middleware — perimeter `X-BSS-API-Token` auth.
//!
//! Rust port of `packages/bss-middleware`. Two pieces:
//! - [`TokenMap`] — an immutable, HMAC-hashed `{token → service_identity}` map
//!   loaded from env once at startup, with constant-time lookup;
//! - [`require_api_token`] — the axum middleware that gates every request and
//!   stamps the resolved [`bss_context::ServiceIdentity`] for the context layer.
//!
//! HMAC hashing + identity derivation are pinned to the Python oracle by golden
//! vectors (`tests/golden_vectors.json`, risk R4).
#![forbid(unsafe_code)]

mod layer;
mod otel;
mod token_map;

pub use layer::{require_api_token, AUTH_INVALID_TOKEN, AUTH_MISSING_TOKEN};
pub use otel::otel_http_span;
pub use token_map::{
    hash_token, identity_from_env_var, load_token_map, validate_token_map,
    validate_token_map_present, TokenMap, TokenMapInvalid,
};

/// The deterministic test token used across the Python test-suite
/// (`bss_middleware.TEST_TOKEN`). Re-exported so Rust tests share the constant.
pub const TEST_TOKEN: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
