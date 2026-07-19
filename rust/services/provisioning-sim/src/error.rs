//! HTTP error mapping — port of the ASGI middleware catches + route 404s.
//!
//! `PolicyViolation` → the frozen 422 body (delegated to `bss_db`); a route 404
//! → `404 {detail}`; an unhandled DB/internal fault → FastAPI's default
//! `500 {detail:"Internal Server Error"}`.

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use bss_db::PolicyViolation;
use serde_json::json;

#[derive(Debug)]
pub enum ApiError {
    Policy(PolicyViolation),
    NotFound(String),
    Internal,
}

impl From<PolicyViolation> for ApiError {
    fn from(p: PolicyViolation) -> Self {
        ApiError::Policy(p)
    }
}

impl From<sqlx::Error> for ApiError {
    fn from(e: sqlx::Error) -> Self {
        tracing::error!(error = %e, "provisioning.db_error");
        ApiError::Internal
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        match self {
            ApiError::Policy(p) => p.into_response(),
            ApiError::NotFound(detail) => {
                (StatusCode::NOT_FOUND, Json(json!({ "detail": detail }))).into_response()
            }
            ApiError::Internal => (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "detail": "Internal Server Error" })),
            )
                .into_response(),
        }
    }
}
