//! HTTP error mapping — the axum equivalent of `RequestIdMiddleware`'s catches.
//!
//! The Python ASGI middleware turns a `PolicyViolation` into the frozen `422
//! {code, reason, message, referenceError, context}` envelope; route-level
//! `HTTPException(404)` renders `{detail}`. Handlers here return
//! `Result<_, ApiError>` and this `IntoResponse` reproduces those shapes.

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use bss_db::PolicyViolation;
use serde_json::json;

#[derive(Debug)]
pub enum ApiError {
    /// `422 POLICY_VIOLATION` — the frozen structured contract.
    Policy(PolicyViolation),
    /// `404 {detail}` — resource not found (message carries the id).
    NotFound(String),
    /// `500 {detail}` — DB/transport fault; not a domain outcome.
    Internal(String),
}

impl From<PolicyViolation> for ApiError {
    fn from(pv: PolicyViolation) -> Self {
        ApiError::Policy(pv)
    }
}

impl From<sqlx::Error> for ApiError {
    fn from(e: sqlx::Error) -> Self {
        ApiError::Internal(e.to_string())
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        match self {
            ApiError::Policy(pv) => pv.into_response(),
            ApiError::NotFound(detail) => {
                (StatusCode::NOT_FOUND, Json(json!({ "detail": detail }))).into_response()
            }
            ApiError::Internal(detail) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "detail": detail })),
            )
                .into_response(),
        }
    }
}
