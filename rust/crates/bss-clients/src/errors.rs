//! Typed client errors — callers branch on the variant, not on JSON parsing.
//!
//! Port of `bss_clients.errors`. The 422 `POLICY_VIOLATION` case reuses
//! [`bss_db::PolicyViolation`] so the same structured error flows unchanged from
//! the server that raised it to the caller that reads it.

use bss_db::PolicyViolation;

/// Every failure mode a [`crate::BssClient`] surfaces.
#[derive(Debug)]
pub enum ClientError {
    /// HTTP 404.
    NotFound(String),
    /// HTTP 422 carrying `code=POLICY_VIOLATION` (the structured domain error).
    Policy(PolicyViolation),
    /// HTTP 5xx. A 503 is a fact — never retried (doctrine).
    Server { status: u16, detail: String },
    /// Other non-2xx (non-policy 422, 4xx like 400/401/403) — the
    /// `raise_for_status` equivalent.
    Http { status: u16, detail: String },
    /// The request timed out (mandatory per-request timeout elapsed).
    Timeout(String),
    /// Connection/transport error that isn't a timeout.
    Transport(String),
}

impl ClientError {
    /// The HTTP status this error corresponds to, matching the Python
    /// `ClientError.status_code` (Timeout → 504, transport → 0).
    pub fn status_code(&self) -> u16 {
        match self {
            ClientError::NotFound(_) => 404,
            ClientError::Policy(_) => 422,
            ClientError::Server { status, .. } | ClientError::Http { status, .. } => *status,
            ClientError::Timeout(_) => 504,
            ClientError::Transport(_) => 0,
        }
    }
}

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClientError::NotFound(d) => write!(f, "not found: {d}"),
            ClientError::Policy(p) => write!(f, "policy violation: {p}"),
            ClientError::Server { status, detail } => write!(f, "server error {status}: {detail}"),
            ClientError::Http { status, detail } => write!(f, "http {status}: {detail}"),
            ClientError::Timeout(d) => write!(f, "timeout: {d}"),
            ClientError::Transport(d) => write!(f, "transport error: {d}"),
        }
    }
}

impl std::error::Error for ClientError {}
