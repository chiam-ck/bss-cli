//! HTTP surface — port of `app.api.subscription` + `renewal_admin` + health.
//! axum 0.7 path params `:name`. Only `/health` is perimeter-exempt (`/ready`
//! requires a token, like the oracle).

use axum::{
    body::Bytes,
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Extension, Json, Router,
};
use bss_context::RequestCtx;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::schemas::{
    to_balance_response, to_subscription_response, MigratePriceRequest, SchedulePlanChangeRequest,
    SubscriptionCreateRequest, TerminateRequest, VasPurchaseRequest,
};
use crate::state::AppState;
use crate::{service, worker};

const SUB: &str = "/subscription-api/v1";

pub fn health_router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
}

pub fn subscription_router() -> Router<AppState> {
    Router::new()
        .route(&format!("{SUB}/subscription"), post(create).get(list))
        .route(&format!("{SUB}/subscription/:sub_id"), get(get_one))
        .route(
            &format!("{SUB}/subscription/by-msisdn/:msisdn"),
            get(get_by_msisdn),
        )
        .route(
            &format!("{SUB}/subscription/:sub_id/balance"),
            get(get_balance),
        )
        .route(
            &format!("{SUB}/subscription/:sub_id/vas-purchase"),
            post(vas_purchase),
        )
        .route(&format!("{SUB}/subscription/:sub_id/renew"), post(renew))
        .route(
            &format!("{SUB}/subscription/:sub_id/terminate"),
            post(terminate),
        )
        .route(
            &format!("{SUB}/subscription/:sub_id/schedule-plan-change"),
            post(schedule_plan_change),
        )
        .route(
            &format!("{SUB}/subscription/:sub_id/cancel-plan-change"),
            post(cancel_plan_change),
        )
        .route(
            &format!("{SUB}/admin/subscription/migrate-price"),
            post(migrate_price),
        )
}

/// Admin extras carrying AppState (nested under `/admin-api/v1`): the v0.18
/// deterministic renewal tick.
pub fn admin_extra_router() -> Router<AppState> {
    Router::new().route("/renewal/tick-now", post(tick_now))
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

async fn create(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(body): Json<SubscriptionCreateRequest>,
) -> Result<impl IntoResponse, ApiError> {
    let full = service::create(&s, &ctx, body).await?;
    Ok((StatusCode::CREATED, Json(to_subscription_response(&full))))
}

async fn get_one(
    State(s): State<AppState>,
    Path(sub_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match service::get(&s.pool, &sub_id).await? {
        Some(full) => Ok(Json(to_subscription_response(&full))),
        None => Err(ApiError::NotFound(format!(
            "Subscription {sub_id} not found"
        ))),
    }
}

#[derive(Debug, Deserialize)]
struct ListQuery {
    #[serde(rename = "customerId")]
    customer_id: Option<String>,
}

async fn list(
    State(s): State<AppState>,
    Query(q): Query<ListQuery>,
) -> Result<Json<Value>, ApiError> {
    let Some(customer_id) = q.customer_id.filter(|c| !c.is_empty()) else {
        return Err(ApiError::BadRequest(
            "customerId query param is required".into(),
        ));
    };
    let subs = service::list_for_customer(&s.pool, &customer_id).await?;
    Ok(Json(Value::Array(
        subs.iter().map(to_subscription_response).collect(),
    )))
}

async fn get_by_msisdn(
    State(s): State<AppState>,
    Path(msisdn): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match service::get_by_msisdn(&s.pool, &msisdn).await? {
        Some(full) => Ok(Json(to_subscription_response(&full))),
        None => Err(ApiError::NotFound(format!(
            "No subscription for MSISDN {msisdn}"
        ))),
    }
}

async fn get_balance(
    State(s): State<AppState>,
    Path(sub_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let balances = service::get(&s.pool, &sub_id)
        .await?
        .map(|f| f.balances)
        .unwrap_or_default();
    if balances.is_empty() {
        return Err(ApiError::NotFound(format!("No balances for {sub_id}")));
    }
    Ok(Json(Value::Array(
        balances.iter().map(to_balance_response).collect(),
    )))
}

async fn vas_purchase(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(sub_id): Path<String>,
    Json(body): Json<VasPurchaseRequest>,
) -> Result<Json<Value>, ApiError> {
    let full = service::purchase_vas(&s, &ctx, &sub_id, &body.vas_offering_id).await?;
    Ok(Json(to_subscription_response(&full)))
}

async fn renew(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(sub_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let full = service::renew(&s, &ctx, &sub_id).await?;
    Ok(Json(to_subscription_response(&full)))
}

async fn terminate(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(sub_id): Path<String>,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    // Optional body (back-compat): empty → customer_requested + release inventory.
    let (reason, release_inventory) = if body.is_empty() {
        ("customer_requested".to_string(), true)
    } else {
        let req: TerminateRequest = serde_json::from_slice(&body)
            .map_err(|e| ApiError::Internal(format!("bad terminate body: {e}")))?;
        (
            req.reason
                .unwrap_or_else(|| "customer_requested".to_string()),
            req.release_inventory,
        )
    };
    let full = service::terminate(&s, &ctx, &sub_id, &reason, release_inventory).await?;
    Ok(Json(to_subscription_response(&full)))
}

async fn schedule_plan_change(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(sub_id): Path<String>,
    Json(body): Json<SchedulePlanChangeRequest>,
) -> Result<Json<Value>, ApiError> {
    let full = service::schedule_plan_change(&s, &ctx, &sub_id, &body.new_offering_id).await?;
    Ok(Json(to_subscription_response(&full)))
}

async fn cancel_plan_change(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(sub_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let full = service::cancel_pending_plan_change(&s, &ctx, &sub_id).await?;
    Ok(Json(to_subscription_response(&full)))
}

async fn migrate_price(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(body): Json<MigratePriceRequest>,
) -> Result<Json<Value>, ApiError> {
    let result = service::migrate_subscriptions_to_price(
        &s,
        &ctx,
        &body.offering_id,
        &body.new_price_id,
        body.effective_from,
        body.notice_days,
        &body.initiated_by,
    )
    .await?;
    Ok(Json(json!({
        "count": result.count,
        "subscriptionIds": result.subscription_ids,
    })))
}

async fn tick_now(State(s): State<AppState>) -> Result<Json<Value>, ApiError> {
    if !admin_reset_allowed() {
        return Err(ApiError::Forbidden(json!({
            "code": "ADMIN_RENEWAL_DISABLED",
            "message": "Renewal admin tick is gated behind BSS_ALLOW_ADMIN_RESET. Set this env to true in dev/test only; production runs the worker on its natural BSS_RENEWAL_TICK_SECONDS interval.",
        })));
    }
    tracing::info!("renewal.admin.tick_now.invoked");
    worker::sweep_due(&s).await?;
    worker::sweep_skipped(&s).await?;
    Ok(Json(json!({ "status": "ok" })))
}

/// Mirrors `bss_admin.reset._is_allowed` — same flag, same truthy set.
fn admin_reset_allowed() -> bool {
    matches!(
        std::env::var("BSS_ALLOW_ADMIN_RESET")
            .unwrap_or_default()
            .trim()
            .to_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}
