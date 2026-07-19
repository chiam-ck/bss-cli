//! HTTP surface — port of `app.api.tmf.*` + `app.api.admin` + health. axum 0.7
//! path params use `:name`. Only `/health` is perimeter-exempt (the oracle's
//! `/ready` requires a token too). Route handlers hold no business logic: they
//! call `service::*` and map `ApiError` onto the frozen envelopes.

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    routing::{get, post},
    Extension, Json, Router,
};
use bss_context::RequestCtx;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::schemas::{
    to_payment_attempt_response, to_payment_method_response, PaymentChargeRequest,
    PaymentMethodCreateRequest,
};
use crate::service;
use crate::state::AppState;

const PAYMENT: &str = "/tmf-api/paymentManagement/v4";
const PAYMENT_METHOD: &str = "/tmf-api/paymentMethodManagement/v4";

// ── health ───────────────────────────────────────────────────────────

pub fn health_router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
}

async fn health(State(s): State<AppState>) -> Json<Value> {
    Json(
        json!({ "status": "ok", "service": s.settings.service_name, "version": s.settings.version }),
    )
}

async fn ready(State(s): State<AppState>) -> Json<Value> {
    match sqlx::query("SELECT 1").execute(&s.pool).await {
        Ok(_) => Json(json!({ "status": "ready", "service": s.settings.service_name })),
        Err(_) => Json(json!({ "status": "unavailable", "service": s.settings.service_name })),
    }
}

// ── payment (TMF676 Payment Management) ──────────────────────────────

pub fn payment_router() -> Router<AppState> {
    Router::new()
        .route(
            &format!("{PAYMENT}/payment"),
            post(charge).get(list_payments),
        )
        .route(&format!("{PAYMENT}/payment/count"), get(count_payments))
        .route(&format!("{PAYMENT}/payment/:attempt_id"), get(get_payment))
}

async fn charge(
    State(state): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(body): Json<PaymentChargeRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let attempt = service::charge(
        &state,
        &ctx,
        &body.customer_id,
        &body.payment_method_id,
        body.amount,
        &body.currency,
        &body.purpose,
    )
    .await?;
    Ok((
        StatusCode::CREATED,
        Json(to_payment_attempt_response(&attempt)),
    ))
}

#[derive(Debug, Deserialize)]
struct CustomerQuery {
    #[serde(rename = "customerId")]
    customer_id: String,
    limit: Option<i64>,
    offset: Option<i64>,
}

async fn list_payments(
    State(state): State<AppState>,
    Query(q): Query<CustomerQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = crate::repo::list_attempts(&state.pool, &q.customer_id, q.limit, q.offset).await?;
    let out: Vec<Value> = rows.iter().map(to_payment_attempt_response).collect();
    Ok(Json(Value::Array(out)))
}

#[derive(Debug, Deserialize)]
struct CountQuery {
    #[serde(rename = "customerId")]
    customer_id: String,
}

async fn count_payments(
    State(state): State<AppState>,
    Query(q): Query<CountQuery>,
) -> Result<Json<Value>, ApiError> {
    let n = crate::repo::count_attempts(&state.pool, &q.customer_id).await?;
    Ok(Json(json!({ "count": n })))
}

async fn get_payment(
    State(state): State<AppState>,
    Path(attempt_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let row = crate::repo::get_attempt(&state.pool, &attempt_id)
        .await?
        .ok_or_else(|| ApiError::NotFound(format!("Payment attempt {attempt_id} not found")))?;
    Ok(Json(to_payment_attempt_response(&row)))
}

// ── paymentMethod (TMF676 Payment Method Management) ─────────────────

pub fn payment_method_router() -> Router<AppState> {
    Router::new()
        .route(
            &format!("{PAYMENT_METHOD}/paymentMethod"),
            post(create_payment_method).get(list_payment_methods),
        )
        .route(
            &format!("{PAYMENT_METHOD}/paymentMethod/:pm_id"),
            get(get_payment_method).delete(remove_payment_method),
        )
        .route(
            &format!("{PAYMENT_METHOD}/paymentMethod/:pm_id/setDefault"),
            post(set_default_payment_method),
        )
}

async fn create_payment_method(
    State(state): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(body): Json<PaymentMethodCreateRequest>,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let provider = body.tokenization_provider.clone();
    let pm = service::register_method(&state, &ctx, body).await?;
    Ok((
        StatusCode::CREATED,
        Json(to_payment_method_response(&pm, Some(&provider))),
    ))
}

async fn list_payment_methods(
    State(state): State<AppState>,
    Query(q): Query<CountQuery>,
) -> Result<Json<Value>, ApiError> {
    let rows = crate::repo::list_methods(&state.pool, &q.customer_id, false).await?;
    let out: Vec<Value> = rows
        .iter()
        .map(|m| to_payment_method_response(m, None))
        .collect();
    Ok(Json(Value::Array(out)))
}

async fn get_payment_method(
    State(state): State<AppState>,
    Path(pm_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let pm = crate::repo::get_method(&state.pool, &pm_id)
        .await?
        .ok_or_else(|| ApiError::NotFound(format!("Payment method {pm_id} not found")))?;
    Ok(Json(to_payment_method_response(&pm, None)))
}

async fn remove_payment_method(
    State(state): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(pm_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let pm = service::remove_method(&state, &ctx, &pm_id).await?;
    Ok(Json(to_payment_method_response(&pm, None)))
}

async fn set_default_payment_method(
    State(state): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(pm_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let pm = service::set_default_method(&state, &ctx, &pm_id).await?;
    Ok(Json(to_payment_method_response(&pm, None)))
}

// ── admin (cutover + ensure_customer) ────────────────────────────────

#[derive(Debug, Deserialize)]
struct DryRunQuery {
    #[serde(rename = "dryRun", default)]
    dry_run: bool,
}

#[derive(Debug, Deserialize)]
struct EnsureCustomerBody {
    #[serde(alias = "customerId")]
    customer_id: String,
    email: String,
}

/// The payment-specific admin routes that sit alongside the shared reset router
/// (mounted together under `/admin-api/v1` in `create_app`).
pub fn admin_extra_router() -> Router<AppState> {
    Router::new()
        .route("/cutover/invalidate-mock-tokens", post(cutover))
        .route("/payment-customer/ensure", post(ensure_customer))
}

async fn cutover(
    State(state): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Query(q): Query<DryRunQuery>,
) -> Result<Json<Value>, ApiError> {
    let result = service::cutover_invalidate_mock_tokens(&state, &ctx, q.dry_run).await?;
    Ok(Json(result))
}

async fn ensure_customer(
    State(state): State<AppState>,
    Json(body): Json<EnsureCustomerBody>,
) -> Result<Json<Value>, ApiError> {
    let cus = state
        .tokenizer
        .ensure_customer(&body.customer_id, &body.email)
        .await
        .map_err(|e| ApiError::Internal(e.to_string()))?;
    let provider = if state.tokenizer.class_name() == "StripeTokenizerAdapter" {
        "stripe"
    } else {
        "mock"
    };
    Ok(Json(
        json!({ "customer_external_ref": cus, "provider": provider }),
    ))
}

// ── webhooks (exempt from the token perimeter) ───────────────────────

pub fn webhooks_router() -> Router<AppState> {
    Router::new().route("/webhooks/stripe", post(crate::webhooks::webhook_stripe))
}
