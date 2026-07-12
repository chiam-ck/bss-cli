//! HTTP error mapping — the axum equivalent of `RequestIdMiddleware`'s catches.
//!
//! `PolicyViolation` → frozen `422` envelope; route `HTTPException(404)` →
//! `{detail}`. Handlers return `Result<_, ApiError>`.

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use bss_db::PolicyViolation;
use serde_json::{json, Value};

#[derive(Debug)]
pub enum ApiError {
    Policy(PolicyViolation),
    NotFound(String),
    /// `HTTPException(400, detail=str)` → `{ "detail": str }`.
    BadRequest(String),
    /// `HTTPException(403, detail=obj)` → `{ "detail": obj }` (the renewal-tick gate).
    Forbidden(Value),
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
            ApiError::BadRequest(detail) => {
                (StatusCode::BAD_REQUEST, Json(json!({ "detail": detail }))).into_response()
            }
            ApiError::Forbidden(detail) => {
                (StatusCode::FORBIDDEN, Json(json!({ "detail": detail }))).into_response()
            }
            ApiError::Internal(detail) => (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(json!({ "detail": detail })),
            )
                .into_response(),
        }
    }
}
