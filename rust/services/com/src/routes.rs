//! HTTP surface — port of `app.api.order` + health. axum 0.7 path params `:name`.
//! Only `/health` is perimeter-exempt (`/ready` requires a token, like the oracle).

use axum::{
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
use crate::schemas::to_product_order;
use crate::service::{self, CreateOrder};
use crate::state::AppState;

const ORDER: &str = "/tmf-api/productOrderingManagement/v4";

pub fn health_router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
}

pub fn order_router() -> Router<AppState> {
    Router::new()
        .route(
            &format!("{ORDER}/productOrder"),
            post(create_order).get(list_orders),
        )
        .route(&format!("{ORDER}/productOrder/:id"), get(get_order))
        .route(
            &format!("{ORDER}/productOrder/:id/submit"),
            post(submit_order),
        )
        .route(
            &format!("{ORDER}/productOrder/:id/cancel"),
            post(cancel_order),
        )
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

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct CreateOrderBody {
    #[serde(alias = "customer_id")]
    customer_id: String,
    #[serde(alias = "offering_id")]
    offering_id: String,
    #[serde(alias = "msisdn_preference", default)]
    msisdn_preference: Option<String>,
    #[serde(default)]
    notes: Option<String>,
    #[serde(alias = "discount_code", default)]
    discount_code: Option<String>,
    #[serde(alias = "skip_assigned_offer", default)]
    skip_assigned_offer: bool,
}

async fn create_order(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Json(b): Json<CreateOrderBody>,
) -> Result<impl IntoResponse, ApiError> {
    let full = service::create_order(
        &s.pool,
        &s.crm,
        &s.catalog,
        &s.payment,
        &ctx,
        CreateOrder {
            customer_id: b.customer_id,
            offering_id: b.offering_id,
            msisdn_preference: b.msisdn_preference,
            notes: b.notes,
            discount_code: b.discount_code,
            skip_assigned_offer: b.skip_assigned_offer,
        },
    )
    .await?;
    Ok((StatusCode::CREATED, Json(to_product_order(&full))))
}

async fn submit_order(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let full = service::submit_order(&s.pool, &s.payment, &ctx, &id).await?;
    Ok(Json(to_product_order(&full)))
}

async fn cancel_order(
    State(s): State<AppState>,
    Extension(ctx): Extension<RequestCtx>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let full = service::cancel_order(&s.pool, &s.som, &ctx, &id).await?;
    Ok(Json(to_product_order(&full)))
}

async fn get_order(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match service::get_order(&s.pool, &id).await? {
        Some(full) => Ok(Json(to_product_order(&full))),
        None => Err(ApiError::NotFound(format!("Order {id} not found"))),
    }
}

#[derive(Debug, Deserialize)]
struct ListQuery {
    #[serde(rename = "customerId")]
    customer_id: Option<String>,
    state: Option<String>,
    #[serde(default = "def_50")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}
fn def_50() -> i64 {
    50
}

async fn list_orders(
    State(s): State<AppState>,
    Query(q): Query<ListQuery>,
) -> Result<Json<Value>, ApiError> {
    let orders = service::list_orders(
        &s.pool,
        q.customer_id.as_deref(),
        q.state.as_deref(),
        q.limit,
        q.offset,
    )
    .await?;
    Ok(Json(Value::Array(
        orders.iter().map(to_product_order).collect(),
    )))
}
