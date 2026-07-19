//! Public dataclasses returned from the auth service surface. Port of
//! `bss_portal_auth.types`.
//!
//! Deliberately small, free of DB types — the portal touches these, never the
//! sqlx rows. Failure shapes are return values (not errors) for ergonomic flow;
//! `RateLimitExceeded` is the one blocking condition modelled as an error.

use chrono::{DateTime, Utc};

/// Returned by `start_email_login` — non-secret state only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoginChallenge {
    pub identity_id: String,
    pub expires_at: DateTime<Utc>,
}

/// Read-only projection of an identity row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IdentityView {
    pub id: String,
    pub email: String,
    pub customer_id: Option<String>,
    pub email_verified_at: Option<DateTime<Utc>>,
    pub status: String,
}

/// Read-only projection of a session row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionView {
    pub id: String,
    pub identity_id: String,
    pub issued_at: DateTime<Utc>,
    pub expires_at: DateTime<Utc>,
    pub last_seen_at: DateTime<Utc>,
}

/// Returned by `start_step_up` — non-secret state only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StepUpChallenge {
    pub session_id: String,
    pub action_label: String,
    pub expires_at: DateTime<Utc>,
}

/// One-shot grant returned by `verify_step_up`. `token` is the plaintext the
/// portal forwards on the next sensitive request; stored hashed, the plaintext
/// only exists in this in-memory object.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StepUpToken {
    pub token: String,
    pub expires_at: DateTime<Utc>,
    pub action_label: String,
}

/// Verify did not produce a session. `reason` is a stable token:
/// `wrong_code` | `expired` | `no_active_token` | `no_such_identity`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoginFailed {
    pub reason: String,
}

/// Step-up verify did not produce a token. `reason`:
/// `wrong_code` | `expired` | `no_active_token` | `wrong_action`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StepUpFailed {
    pub reason: String,
}

/// Raised when a configured per-email / per-IP / per-session rate window is
/// exceeded. `retry_after_seconds` is the wait before the *oldest* attempt in
/// the window expires (log/debug context — the portal shows a generic message).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RateLimitExceeded {
    pub retry_after_seconds: i64,
    pub scope: String,
}

impl std::fmt::Display for RateLimitExceeded {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "rate limit exceeded for {} — retry in {}s",
            self.scope, self.retry_after_seconds
        )
    }
}
impl std::error::Error for RateLimitExceeded {}
