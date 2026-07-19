//! HTTP error mapping — the axum equivalent of `RequestIdMiddleware`'s catches.
//!
//! The Python ASGI middleware wraps the whole app and turns a `RatingError` into
//! `422 {code:"RATING_ERROR", message}` and an upstream `ServerError` into
//! `500 {detail:"Upstream service error"}`. In Rust the handlers return
//! `Result<_, ApiError>`; this `IntoResponse` reproduces those wire shapes.

use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;

use crate::domain::RatingError;

#[derive(Debug)]
pub enum ApiError {
    /// `422 {code:"RATING_ERROR", message}` — a usage event that can't be rated.
    Rating(RatingError),
    /// `404 {detail}` — offering not found (message carries the id).
    NotFound(String),
    /// `500 {detail:"Upstream service error"}` — a catalog 5xx / transport fault.
    Upstream,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        match self {
            ApiError::Rating(RatingError(message)) => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({ "code": "RATING_ERROR", "message": message })),
            )
                .into_response(),
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
