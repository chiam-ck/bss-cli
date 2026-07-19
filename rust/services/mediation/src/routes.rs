//! HTTP surface — health/ready + the TMF635 `/usage` endpoints.
//!
//! Ports `app.api.health` and `app.api.usage`:
//! - `GET /health` → `{status, service, version}` (perimeter-exempt)
//! - `GET /ready`  → DB ping, always 200 with a status field (token-required)
//! - `POST /usage` → ingest (201) via [`crate::service::ingest`]
//! - `GET /usage/{id}` → single event (404 on miss)
//! - `GET /usage` → filtered list

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::repo::{self, ListFilters};
use crate::service::{ingest, IngestRequest};
use crate::state::AppState;

/// Root health routes (`/health`, `/ready`).
pub fn health_router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
}

/// The TMF635 usage-management routes (mounted at `/tmf-api/usageManagement/v4`).
pub fn usage_router() -> Router<AppState> {
    Router::new()
        .route("/usage", post(create_usage).get(list_usage))
        .route("/usage/:event_id", get(get_usage))
}

async fn health(State(s): State<AppState>) -> Json<Value> {
    Json(json!({
        "status": "ok",
        "service": s.settings.service_name,
        "version": s.settings.version,
    }))
}

async fn ready(State(s): State<AppState>) -> Json<Value> {
    match sqlx::query("SELECT 1").execute(&s.pool).await {
        Ok(_) => Json(json!({ "status": "ready", "service": s.settings.service_name })),
        Err(_) => Json(json!({ "status": "unavailable", "service": s.settings.service_name })),
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct UsageCreateRequest {
    msisdn: String,
    event_type: String,
    event_time: DateTime<Utc>,
    quantity: i64,
    unit: String,
    #[serde(default)]
    source: Option<String>,
    #[serde(default)]
    raw_cdr_ref: Option<String>,
    #[serde(default)]
    roaming_indicator: bool,
}

async fn create_usage(
    State(s): State<AppState>,
    Json(body): Json<UsageCreateRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let ctx = bss_context::current();
    let req = IngestRequest {
        msisdn: body.msisdn,
        event_type: body.event_type,
        event_time: body.event_time,
        quantity: body.quantity,
        unit: body.unit,
        source: body.source,
        raw_cdr_ref: body.raw_cdr_ref,
        roaming_indicator: body.roaming_indicator,
    };
    let row = ingest(&s, &ctx, req).await?;
    Ok((StatusCode::CREATED, Json(repo::to_response(&row))))
}

async fn get_usage(
    State(s): State<AppState>,
    Path(event_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get(&s.pool, &event_id).await {
        Ok(Some(row)) => Ok(Json(repo::to_response(&row))),
        Ok(None) => Err(ApiError::NotFound(format!(
            "Usage event {event_id} not found"
        ))),
        Err(_) => Err(ApiError::Upstream),
    }
}

#[derive(Debug, Deserialize)]
struct ListQuery {
    #[serde(rename = "subscriptionId")]
    subscription_id: Option<String>,
    msisdn: Option<String>,
    #[serde(rename = "type")]
    event_type: Option<String>,
    since: Option<DateTime<Utc>>,
    limit: Option<i64>,
}

async fn list_usage(
    State(s): State<AppState>,
    Query(q): Query<ListQuery>,
) -> Result<Json<Value>, ApiError> {
    // Clamp to the Python `Query(ge=1, le=1000)` bounds; default 100.
    let limit = q.limit.unwrap_or(100).clamp(1, 1000);
    let filters = ListFilters {
        subscription_id: q.subscription_id,
        msisdn: q.msisdn,
        event_type: q.event_type,
        since: q.since,
        limit,
    };
    match repo::list_by_filters(&s.pool, &filters).await {
        Ok(rows) => Ok(Json(Value::Array(
            rows.iter().map(repo::to_response).collect(),
        ))),
        Err(_) => Err(ApiError::Upstream),
    }
}
