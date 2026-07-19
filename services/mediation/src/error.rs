//! HTTP error mapping — the axum equivalent of `RequestIdMiddleware`'s catches.
//!
//! The Python ASGI middleware turns a `PolicyViolation` into the frozen 422 body
//! and an upstream `ServerError` into `500 {detail:"Upstream service error"}`.
//! Route/GET handlers additionally raise a 404 `HTTPException`. In Rust the
//! handlers return `Result<_, ApiError>`; this `IntoResponse` reproduces those
//! wire shapes (the 422 is delegated to `bss_db::PolicyViolation`, keeping the
//! frozen five-key contract in one place).

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use bss_db::PolicyViolation;
use serde_json::json;

#[derive(Debug)]
pub enum ApiError {
    /// `422 {code:"POLICY_VIOLATION", reason, message, referenceError, context}`.
    Policy(PolicyViolation),
    /// `404 {detail}` — usage event not found (message carries the id).
    NotFound(String),
    /// `500 {detail:"Upstream service error"}` — a Subscription 5xx / transport fault.
    Upstream,
}

impl From<PolicyViolation> for ApiError {
    fn from(p: PolicyViolation) -> Self {
        ApiError::Policy(p)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        match self {
            ApiError::Policy(p) => p.into_response(),
            ApiError::NotFound(detail) => {
                (StatusCode::NOT_FOUND, Json(json!({ "detail": detail }))).into_response()
            }
            ApiError::Upstream => (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "detail": "Upstream service error" })),
            )
                .into_response(),
        }
    }
}
