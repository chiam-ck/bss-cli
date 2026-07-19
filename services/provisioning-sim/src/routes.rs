//! HTTP surface — health + provisioning task + fault-injection routes.
//!
//! Ports `app.api.health`, `app.api.task`, `app.api.fault_injection`. No business
//! logic in handlers (doctrine): each calls [`crate::service`] or the repo.

use axum::{
    extract::{Path, Query, State},
    routing::{get, patch, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::repo;
use crate::service;
use crate::state::AppState;

pub fn health_router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
}

/// The `/provisioning-api/v1` routes (task + fault-injection).
pub fn provisioning_router() -> Router<AppState> {
    Router::new()
        .route("/task", get(list_tasks))
        .route("/task/:task_id", get(get_task))
        .route("/task/:task_id/resolve", post(resolve_stuck))
        .route("/task/:task_id/retry", post(retry_failed))
        .route("/fault-injection", get(list_fault_rules))
        .route("/fault-injection/:fault_id", patch(update_fault_rule))
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

async fn get_task(
    State(s): State<AppState>,
    Path(task_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::task_get(&s.pool, &task_id).await? {
        Some(t) => Ok(Json(t.to_response())),
        None => Err(ApiError::NotFound(format!("Task {task_id} not found"))),
    }
}

#[derive(Debug, Deserialize)]
struct ListQuery {
    #[serde(rename = "serviceId")]
    service_id: Option<String>,
    state: Option<String>,
}

async fn list_tasks(
    State(s): State<AppState>,
    Query(q): Query<ListQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = repo::task_list(&s.pool, q.service_id.as_deref(), q.state.as_deref()).await?;
    Ok(Json(Value::Array(
        rows.iter().map(repo::TaskRow::to_response).collect(),
    )))
}

#[derive(Debug, Deserialize)]
struct ResolveRequest {
    note: String,
}

async fn resolve_stuck(
    State(s): State<AppState>,
    Path(task_id): Path<String>,
    Json(body): Json<ResolveRequest>,
) -> Result<Json<Value>, ApiError> {
    let ctx = bss_context::current();
    let task = service::resolve_stuck(&s, &ctx, &task_id, &body.note).await?;
    Ok(Json(task))
}

async fn retry_failed(
    State(s): State<AppState>,
    Path(task_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let ctx = bss_context::current();
    let task = service::retry_failed(&s, &ctx, &task_id).await?;
    Ok(Json(task))
}

async fn list_fault_rules(State(s): State<AppState>) -> Result<Json<Value>, ApiError> {
    let rules = service::list_fault_rules(&s).await?;
    Ok(Json(Value::Array(rules)))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FaultUpdateRequest {
    #[serde(default)]
    enabled: Option<bool>,
    #[serde(default)]
    probability: Option<f64>,
    #[serde(default)]
    fault_type: Option<String>,
}

async fn update_fault_rule(
    State(s): State<AppState>,
    Path(fault_id): Path<String>,
    Json(body): Json<FaultUpdateRequest>,
) -> Result<Json<Value>, ApiError> {
    let ctx = bss_context::current();
    let fault = service::update_fault_rule(
        &s,
        &ctx,
        &fault_id,
        body.enabled,
        body.probability,
        body.fault_type,
    )
    .await?;
    Ok(Json(fault))
}
