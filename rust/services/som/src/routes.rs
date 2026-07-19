//! HTTP surface — health + TMF641 ServiceOrder + TMF638 Service reads.
//!
//! Ports `app.api.health`, `app.api.service_order`, `app.api.service`. All reads
//! (SOM writes happen on the event plane, not via HTTP).

use axum::{
    extract::{Path, Query, State},
    routing::get,
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::repo;
use crate::state::AppState;

pub fn health_router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
}

/// TMF641 ServiceOrder routes (mounted at `/tmf-api/serviceOrderingManagement/v4`).
pub fn service_order_router() -> Router<AppState> {
    Router::new()
        .route("/serviceOrder", get(list_service_orders))
        .route("/serviceOrder/:order_id", get(get_service_order))
}

/// TMF638 Service routes (mounted at `/tmf-api/serviceInventoryManagement/v4`).
pub fn service_router() -> Router<AppState> {
    Router::new()
        .route("/service", get(list_services))
        .route("/service/:service_id", get(get_service))
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

async fn get_service_order(
    State(s): State<AppState>,
    Path(order_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_service_order_response(&s.pool, &order_id).await? {
        Some(v) => Ok(Json(v)),
        None => Err(ApiError::NotFound(format!(
            "ServiceOrder {order_id} not found"
        ))),
    }
}

#[derive(Debug, Deserialize)]
struct CommercialQuery {
    #[serde(rename = "commercialOrderId")]
    commercial_order_id: String,
}

async fn list_service_orders(
    State(s): State<AppState>,
    Query(q): Query<CommercialQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::list_service_orders_by_commercial(&s.pool, &q.commercial_order_id).await?;
    Ok(Json(Value::Array(rows)))
}

async fn get_service(
    State(s): State<AppState>,
    Path(service_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_service_response(&s.pool, &service_id).await? {
        Some(v) => Ok(Json(v)),
        None => Err(ApiError::NotFound(format!(
            "Service {service_id} not found"
        ))),
    }
}

#[derive(Debug, Deserialize)]
struct SubscriptionQuery {
    #[serde(rename = "subscriptionId")]
    subscription_id: String,
}

async fn list_services(
    State(s): State<AppState>,
    Query(q): Query<SubscriptionQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::list_services_by_subscription(&s.pool, &q.subscription_id).await?;
    Ok(Json(Value::Array(rows)))
}
