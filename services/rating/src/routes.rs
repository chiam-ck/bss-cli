//! HTTP surface — health/ready + the `/rating-api/v1` debug endpoints.
//!
//! Ports `app.api.health` and `app.api.rating`:
//! - `GET /health`  → `{status, service, version}` (perimeter-exempt)
//! - `GET /ready`   → DB ping, always 200 with a status field (token-required —
//!   only `/health` is in the exempt set)
//! - `GET /rating-api/v1/tariff/{offering_id}` → catalog passthrough (404 on miss)
//! - `POST /rating-api/v1/rate-test` → apply `rate_usage`, no persist

use axum::{
    extract::{Path, State},
    routing::{get, post},
    Json, Router,
};
use bss_clients::ClientError;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::domain::{rate_usage, UsageInput};
use crate::error::ApiError;
use crate::state::AppState;

/// Root health routes (`/health`, `/ready`).
pub fn health_router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
}

/// The `/rating-api/v1` debug/inspection routes.
pub fn rating_router() -> Router<AppState> {
    Router::new()
        .route("/tariff/:offering_id", get(get_tariff))
        .route("/rate-test", post(rate_test))
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

async fn get_tariff(
    State(s): State<AppState>,
    Path(offering_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match s.catalog.get_offering(&offering_id).await {
        Ok(tariff) => Ok(Json(tariff)),
        Err(ClientError::NotFound(_)) => Err(ApiError::NotFound(format!(
            "Offering {offering_id} not found"
        ))),
        Err(_) => Err(ApiError::Upstream),
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RateTestRequest {
    #[serde(default = "default_ue")]
    usage_event_id: String,
    subscription_id: String,
    msisdn: String,
    offering_id: String,
    event_type: String,
    quantity: i64,
    unit: String,
}

fn default_ue() -> String {
    "UE-TEST".to_string()
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct RateTestResponse {
    usage_event_id: String,
    subscription_id: String,
    allowance_type: String,
    consumed_quantity: i64,
    unit: String,
    charge_amount: String,
    currency: String,
}

async fn rate_test(
    State(s): State<AppState>,
    Json(body): Json<RateTestRequest>,
) -> Result<Json<RateTestResponse>, ApiError> {
    let tariff = match s.catalog.get_offering(&body.offering_id).await {
        Ok(t) => t,
        Err(ClientError::NotFound(_)) => {
            return Err(ApiError::NotFound(format!(
                "Offering {} not found",
                body.offering_id
            )))
        }
        Err(_) => return Err(ApiError::Upstream),
    };

    let usage = UsageInput {
        usage_event_id: body.usage_event_id,
        subscription_id: body.subscription_id,
        msisdn: body.msisdn,
        event_type: body.event_type,
        quantity: body.quantity,
        unit: body.unit,
    };
    let result = rate_usage(&usage, &tariff).map_err(ApiError::Rating)?;

    Ok(Json(RateTestResponse {
        usage_event_id: result.usage_event_id,
        subscription_id: result.subscription_id,
        allowance_type: result.allowance_type,
        consumed_quantity: result.consumed_quantity,
        unit: result.unit,
        charge_amount: result.charge_amount,
        currency: result.currency,
    }))
}
