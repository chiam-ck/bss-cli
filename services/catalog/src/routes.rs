//! HTTP surface — port of `bss_catalog.routes.*` + the promotion router.
//!
//! Sub-routers merged in `create_app`. axum 0.7 path params are `:name`. Only
//! `/health` is perimeter-exempt (like the oracle, `/ready` requires a token).

use axum::{
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    routing::{get, patch, post},
    Json, Router,
};
use bss_db::PolicyViolation;
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::promo_service::{self, CreatePromotion, ResolveResult, Terms, ValidateResult};
use crate::services::{self, AddOffering};
use crate::state::AppState;
use crate::{repo, tmf};

const TMF: &str = "/tmf-api/productCatalogManagement/v4";
const TMF_PROMO: &str = "/tmf-api/promotionManagement/v4";

// ── routers ───────────────────────────────────────────────────────────────────

pub fn health_router() -> Router<AppState> {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
}

pub fn tmf_router() -> Router<AppState> {
    Router::new()
        .route(&format!("{TMF}/productOffering"), get(list_offerings))
        .route(&format!("{TMF}/productOffering/:id"), get(get_offering))
        .route(&format!("{TMF}/productOfferingPrice/:id"), get(get_price))
        .route(
            &format!("{TMF}/productOfferingPrice/active/:id"),
            get(get_active_price),
        )
        .route(&format!("{TMF}/productSpecification"), get(list_specs))
        .route(&format!("{TMF}/productSpecification/:id"), get(get_spec))
}

pub fn vas_router() -> Router<AppState> {
    Router::new()
        .route("/vas/offering", get(list_vas))
        .route("/vas/offering/:id", get(get_vas))
}

pub fn admin_router() -> Router<AppState> {
    Router::new()
        .route("/admin/catalog/offering", post(admin_add_offering))
        .route("/admin/catalog/offering/:id/window", patch(admin_window))
        .route("/admin/catalog/offering/:id/retire", post(admin_retire))
        .route("/admin/catalog/offering/:id/price", post(admin_add_price))
}

pub fn promotion_router() -> Router<AppState> {
    Router::new()
        .route(
            &format!("{TMF_PROMO}/promotion"),
            post(create_promotion).get(list_promotions),
        )
        .route(&format!("{TMF_PROMO}/promotion/:id"), get(get_promotion))
        .route(
            &format!("{TMF_PROMO}/promotion/:id/assign"),
            post(assign_targeted),
        )
        .route(
            &format!("{TMF_PROMO}/promotion/:id/unassign"),
            post(unassign_targeted),
        )
        .route(
            &format!("{TMF_PROMO}/promotion/:id/exhaust"),
            post(exhaust_promotion),
        )
        .route("/promo/preview", get(promo_preview))
        .route("/promo/validate", get(promo_validate))
        .route("/promo/resolve-eligible", get(promo_resolve_eligible))
        .route("/promo/customer-offers", get(promo_customer_offers))
}

// ── health ────────────────────────────────────────────────────────────────────

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

// ── TMF620 reads ────────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct ListOfferingsQuery {
    #[serde(rename = "lifecycleStatus")]
    lifecycle_status: Option<String>,
    #[serde(rename = "activeAt")]
    active_at: Option<String>,
    #[serde(default = "def_20")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}
fn def_20() -> i64 {
    20
}
fn def_50() -> i64 {
    50
}

async fn list_offerings(
    State(s): State<AppState>,
    Query(q): Query<ListOfferingsQuery>,
) -> Result<Json<Value>, ApiError> {
    let out = if let Some(active_at) = &q.active_at {
        let moment = parse_dt(active_at)?;
        repo::list_active_offerings(&s.pool, moment, q.limit, q.offset).await?
    } else {
        repo::list_offerings(&s.pool, q.lifecycle_status.as_deref(), q.limit, q.offset).await?
    };
    Ok(Json(Value::Array(
        out.iter().map(tmf::to_tmf620_offering).collect(),
    )))
}

async fn get_offering(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_offering(&s.pool, &id).await? {
        Some(f) => Ok(Json(tmf::to_tmf620_offering(&f))),
        None => Err(ApiError::NotFound(format!(
            "ProductOffering {id} not found"
        ))),
    }
}

async fn get_price(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_price_by_id(&s.pool, &id).await? {
        Some(p) => Ok(Json(tmf::to_tmf620_price(&p))),
        None => Err(ApiError::NotFound(format!(
            "ProductOfferingPrice {id} not found"
        ))),
    }
}

#[derive(Debug, Deserialize)]
struct ActiveAtQuery {
    #[serde(rename = "activeAt")]
    active_at: Option<String>,
}

async fn get_active_price(
    State(s): State<AppState>,
    Path(id): Path<String>,
    Query(q): Query<ActiveAtQuery>,
) -> Result<Json<Value>, ApiError> {
    let moment = match &q.active_at {
        Some(a) => parse_dt(a)?,
        None => bss_clock::now(),
    };
    match repo::active_price(&s.pool, &id, moment).await? {
        Some(p) => Ok(Json(tmf::to_tmf620_price(&p))),
        None => {
            let at = bss_clock::isoformat(moment);
            Err(PolicyViolation::with_context(
                "catalog.price.no_active_row",
                format!("No active recurring price for offering {id} at {at}"),
                json!({ "offering_id": id, "at": at }),
            )
            .into())
        }
    }
}

#[derive(Debug, Deserialize)]
struct PageQuery {
    #[serde(default = "def_20")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

async fn list_specs(
    State(s): State<AppState>,
    Query(q): Query<PageQuery>,
) -> Result<Json<Value>, ApiError> {
    let specs = repo::list_specifications(&s.pool, q.limit, q.offset).await?;
    Ok(Json(Value::Array(
        specs.iter().map(tmf::to_tmf620_spec).collect(),
    )))
}

async fn get_spec(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_spec(&s.pool, &id).await? {
        Some(sp) => Ok(Json(tmf::to_tmf620_spec(&sp))),
        None => Err(ApiError::NotFound(format!(
            "ProductSpecification {id} not found"
        ))),
    }
}

async fn list_vas(
    State(s): State<AppState>,
    Query(q): Query<PageQuery>,
) -> Result<Json<Value>, ApiError> {
    let vas = repo::list_vas(&s.pool, q.limit, q.offset).await?;
    Ok(Json(Value::Array(
        vas.iter().map(tmf::to_vas_offering).collect(),
    )))
}

async fn get_vas(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match repo::get_vas(&s.pool, &id).await? {
        Some(v) => Ok(Json(tmf::to_vas_offering(&v))),
        None => Err(ApiError::NotFound(format!("VAS offering {id} not found"))),
    }
}

// ── admin writes ────────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AddOfferingBody {
    #[serde(alias = "offering_id")]
    offering_id: String,
    name: String,
    #[serde(alias = "spec_id", default = "def_spec")]
    spec_id: String,
    #[serde(deserialize_with = "de_decimal")]
    amount: Decimal,
    #[serde(default = "def_sgd")]
    currency: String,
    #[serde(alias = "price_id", default)]
    price_id: Option<String>,
    #[serde(alias = "valid_from", default)]
    valid_from: Option<String>,
    #[serde(alias = "valid_to", default)]
    valid_to: Option<String>,
    #[serde(alias = "data_mb", default)]
    data_mb: Option<i64>,
    #[serde(alias = "voice_minutes", default)]
    voice_minutes: Option<i64>,
    #[serde(alias = "sms_count", default)]
    sms_count: Option<i64>,
    #[serde(alias = "data_roaming_mb", default)]
    data_roaming_mb: Option<i64>,
}
fn def_spec() -> String {
    "SPEC_MOBILE_PREPAID".to_string()
}
fn def_sgd() -> String {
    "SGD".to_string()
}

async fn admin_add_offering(
    State(s): State<AppState>,
    headers: HeaderMap,
    Json(b): Json<AddOfferingBody>,
) -> Result<Json<Value>, ApiError> {
    let actor = actor_of(&headers);
    let vf = opt_dt(b.valid_from.as_deref())?;
    let vt = opt_dt(b.valid_to.as_deref())?;
    let full = services::add_offering(
        &s.pool,
        &actor,
        AddOffering {
            offering_id: &b.offering_id,
            name: &b.name,
            spec_id: &b.spec_id,
            amount: b.amount,
            currency: &b.currency,
            price_id: b.price_id.as_deref(),
            valid_from: vf,
            valid_to: vt,
            data_mb: b.data_mb,
            voice_minutes: b.voice_minutes,
            sms_count: b.sms_count,
            data_roaming_mb: b.data_roaming_mb,
        },
    )
    .await?;
    Ok(Json(tmf::to_tmf620_offering(&full)))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct WindowBody {
    #[serde(alias = "valid_from", default)]
    valid_from: Option<String>,
    #[serde(alias = "valid_to", default)]
    valid_to: Option<String>,
}

async fn admin_window(
    State(s): State<AppState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(b): Json<WindowBody>,
) -> Result<Json<Value>, ApiError> {
    let actor = actor_of(&headers);
    let vf = opt_dt(b.valid_from.as_deref())?;
    let vt = opt_dt(b.valid_to.as_deref())?;
    let full = services::set_offering_window(&s.pool, &actor, &id, vf, vt).await?;
    Ok(Json(tmf::to_tmf620_offering(&full)))
}

async fn admin_retire(
    State(s): State<AppState>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let actor = actor_of(&headers);
    let full = services::retire_offering(&s.pool, &actor, &id).await?;
    Ok(Json(tmf::to_tmf620_offering(&full)))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AddPriceBody {
    #[serde(alias = "price_id")]
    price_id: String,
    #[serde(deserialize_with = "de_decimal")]
    amount: Decimal,
    #[serde(default = "def_sgd")]
    currency: String,
    #[serde(alias = "valid_from", default)]
    valid_from: Option<String>,
    #[serde(alias = "valid_to", default)]
    valid_to: Option<String>,
    #[serde(alias = "retire_current", default)]
    retire_current: bool,
}

async fn admin_add_price(
    State(s): State<AppState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(b): Json<AddPriceBody>,
) -> Result<Json<Value>, ApiError> {
    let actor = actor_of(&headers);
    let vf = opt_dt(b.valid_from.as_deref())?;
    let vt = opt_dt(b.valid_to.as_deref())?;
    let price = services::add_price(
        &s.pool,
        &actor,
        &id,
        &b.price_id,
        b.amount,
        &b.currency,
        vf,
        vt,
        b.retire_current,
    )
    .await?;
    Ok(Json(tmf::to_tmf620_price(&price)))
}

// ── promotion writes/reads (TMF671) ─────────────────────────────────────────────

fn to_tmf671(p: &crate::promo_repo::PromotionRow) -> Value {
    json!({
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "audience": p.audience,
        "offerDefinitionId": p.offer_definition_id,
        "discountType": p.discount_type,
        "discountValue": p.discount_value.to_string(),
        "currency": p.currency,
        "applicableOfferingIds": p.applicable_offering_ids,
        "durationKind": p.duration_kind,
        "periodsTotal": p.periods_total,
        "validFrom": p.valid_from.map(tmf::tmf_datetime),
        "validTo": p.valid_to.map(tmf::tmf_datetime),
        "state": p.state,
        "@type": "Promotion",
    })
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct CreatePromotionBody {
    #[serde(alias = "promotion_id")]
    promotion_id: String,
    #[serde(alias = "discount_type")]
    discount_type: String,
    #[serde(alias = "discount_value", deserialize_with = "de_decimal")]
    discount_value: Decimal,
    #[serde(alias = "duration_kind")]
    duration_kind: String,
    #[serde(default = "def_public")]
    audience: String,
    #[serde(default = "def_sgd")]
    currency: String,
    #[serde(default)]
    code: Option<String>,
    #[serde(alias = "promo_code_kind", default)]
    promo_code_kind: Option<String>,
    #[serde(alias = "applicable_offering_ids", default)]
    applicable_offering_ids: Option<Vec<String>>,
    #[serde(alias = "periods_total", default)]
    periods_total: Option<i16>,
    #[serde(alias = "valid_from", default)]
    valid_from: Option<String>,
    #[serde(alias = "valid_to", default)]
    valid_to: Option<String>,
    #[serde(alias = "display_name", default)]
    display_name: Option<String>,
}
fn def_public() -> String {
    "public".to_string()
}

async fn create_promotion(
    State(s): State<AppState>,
    headers: HeaderMap,
    Json(b): Json<CreatePromotionBody>,
) -> Result<impl IntoResponse, ApiError> {
    let actor = actor_of(&headers);
    let req = CreatePromotion {
        promotion_id: b.promotion_id,
        discount_type: b.discount_type,
        discount_value: b.discount_value,
        duration_kind: b.duration_kind,
        audience: b.audience,
        currency: b.currency,
        code: b.code,
        promo_code_kind: b.promo_code_kind,
        applicable_offering_ids: b.applicable_offering_ids,
        periods_total: b.periods_total,
        valid_from: opt_dt(b.valid_from.as_deref())?,
        valid_to: opt_dt(b.valid_to.as_deref())?,
        display_name: b.display_name,
    };
    let promo = promo_service::create_promotion(&s.pool, s.loyalty.as_ref(), &actor, req).await?;
    Ok((StatusCode::CREATED, Json(to_tmf671(&promo))))
}

async fn get_promotion(
    State(s): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    match promo_service::get(&s.pool, &id).await? {
        Some(p) => Ok(Json(to_tmf671(&p))),
        None => Err(ApiError::NotFound(format!("Promotion {id} not found"))),
    }
}

#[derive(Debug, Deserialize)]
struct ListPromotionsQuery {
    state: Option<String>,
    #[serde(default = "def_50")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

async fn list_promotions(
    State(s): State<AppState>,
    Query(q): Query<ListPromotionsQuery>,
) -> Result<Json<Value>, ApiError> {
    let promos =
        promo_service::list_promotions(&s.pool, q.state.as_deref(), q.limit, q.offset).await?;
    Ok(Json(Value::Array(promos.iter().map(to_tmf671).collect())))
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct CustomerIdsBody {
    #[serde(alias = "customer_ids")]
    customer_ids: Vec<String>,
}

async fn assign_targeted(
    State(s): State<AppState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(b): Json<CustomerIdsBody>,
) -> Result<Json<Value>, ApiError> {
    let actor = actor_of(&headers);
    let out =
        promo_service::assign_targeted(&s.pool, s.loyalty.as_ref(), &actor, &id, &b.customer_ids)
            .await?;
    Ok(Json(out))
}

async fn unassign_targeted(
    State(s): State<AppState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(b): Json<CustomerIdsBody>,
) -> Result<Json<Value>, ApiError> {
    let actor = actor_of(&headers);
    let out =
        promo_service::unassign_targeted(&s.pool, s.loyalty.as_ref(), &actor, &id, &b.customer_ids)
            .await?;
    Ok(Json(out))
}

async fn exhaust_promotion(
    State(s): State<AppState>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let actor = actor_of(&headers);
    match promo_service::exhaust_promotion(&s.pool, &actor, &id).await {
        Ok(p) => Ok(Json(to_tmf671(&p))),
        // The service raises catalog.promotion.not_found → route maps to 404.
        Err(ApiError::Policy(pv)) if pv.rule == "catalog.promotion.not_found" => {
            Err(ApiError::NotFound(format!("Promotion {id} not found")))
        }
        Err(e) => Err(e),
    }
}

// ── promotion portal-facing reads ────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct PreviewQuery {
    code: String,
    offering: String,
    #[serde(rename = "customerId")]
    customer_id: Option<String>,
}

async fn promo_preview(
    State(s): State<AppState>,
    Query(q): Query<PreviewQuery>,
) -> Result<Json<Value>, ApiError> {
    let r = promo_service::validate_for_order(
        &s.pool,
        s.loyalty.as_ref(),
        &q.code,
        &q.offering,
        q.customer_id.as_deref(),
    )
    .await?;
    let (base, effective, name) = terms_display(&r.terms);
    Ok(Json(json!({
        "valid": r.valid,
        "code": q.code,
        "offering": q.offering,
        "label": r.terms.as_ref().map(|t| t.label.clone()),
        "name": name,
        "base": base,
        "effective": effective,
        "reason": r.reason,
    })))
}

async fn promo_validate(
    State(s): State<AppState>,
    Query(q): Query<PreviewQuery>,
) -> Result<Json<Value>, ApiError> {
    let r = promo_service::validate_for_order(
        &s.pool,
        s.loyalty.as_ref(),
        &q.code,
        &q.offering,
        q.customer_id.as_deref(),
    )
    .await?;
    Ok(Json(validate_wire(&q.code, &q.offering, &r)))
}

#[derive(Debug, Deserialize)]
struct ResolveQuery {
    #[serde(rename = "customerId")]
    customer_id: String,
    offering: String,
}

async fn promo_resolve_eligible(
    State(s): State<AppState>,
    Query(q): Query<ResolveQuery>,
) -> Result<Json<Value>, ApiError> {
    let r = promo_service::resolve_eligible_promo(
        &s.pool,
        s.loyalty.as_ref(),
        &q.customer_id,
        &q.offering,
    )
    .await?;
    if !r.valid {
        return Ok(Json(json!({ "valid": false, "reason": r.reason })));
    }
    Ok(Json(resolve_wire(&r)))
}

#[derive(Debug, Deserialize)]
struct CustomerOffersQuery {
    #[serde(rename = "customerId")]
    customer_id: String,
    #[allow(dead_code)]
    state: Option<String>,
}

async fn promo_customer_offers(
    State(s): State<AppState>,
    Query(q): Query<CustomerOffersQuery>,
) -> Result<Json<Value>, ApiError> {
    let offers =
        promo_service::list_customer_offers(&s.pool, s.loyalty.as_ref(), &q.customer_id).await?;
    Ok(Json(
        json!({ "customerId": q.customer_id, "offers": offers }),
    ))
}

// ── wire helpers ──────────────────────────────────────────────────────────────

fn terms_display(terms: &Option<Terms>) -> (Value, Value, Value) {
    match terms {
        Some(t) => (
            json!(t.base.to_string()),
            json!(t.effective.to_string()),
            t.name.clone().map(Value::from).unwrap_or(Value::Null),
        ),
        None => (Value::Null, Value::Null, Value::Null),
    }
}

fn validate_wire(code: &str, offering: &str, r: &ValidateResult) -> Value {
    let t = r.terms.as_ref();
    json!({
        "valid": r.valid,
        "code": code,
        "offering": offering,
        "reason": r.reason,
        "name": t.and_then(|t| t.name.clone()),
        "offerDefinitionId": r.offer_definition_id,
        "loyaltyOfferId": r.loyalty_offer_id,
        "discountType": t.map(|t| t.discount_type.clone()),
        "discountValue": t.map(|t| t.discount_value.to_string()),
        "durationKind": t.map(|t| t.duration_kind.clone()),
        "periodsTotal": t.and_then(|t| t.periods_total),
        "discountPeriodsTotal": t.map(|t| t.discount_periods_total),
        "base": t.map(|t| t.base.to_string()),
        "effective": t.map(|t| t.effective.to_string()),
        "label": t.map(|t| t.label.clone()),
    })
}

fn resolve_wire(r: &ResolveResult) -> Value {
    let t = r.terms.as_ref();
    json!({
        "valid": true,
        "code": r.code,
        "promotionId": r.promotion_id,
        "name": t.and_then(|t| t.name.clone()),
        "offerDefinitionId": r.offer_definition_id,
        "loyaltyOfferId": r.loyalty_offer_id,
        "discountType": t.map(|t| t.discount_type.clone()),
        "discountValue": t.map(|t| t.discount_value.to_string()),
        "durationKind": t.map(|t| t.duration_kind.clone()),
        "periodsTotal": t.and_then(|t| t.periods_total),
        "discountPeriodsTotal": t.map(|t| t.discount_periods_total),
        "base": t.map(|t| t.base.to_string()),
        "effective": t.map(|t| t.effective.to_string()),
        "label": t.map(|t| t.label.clone()),
    })
}

// ── extractors / parsing ──────────────────────────────────────────────────────

/// `X-BSS-Actor` header, defaulting to `"anonymous"` (the oracle's admin/promo
/// `Header(default="anonymous")` — `check_admin` rejects it).
fn actor_of(headers: &HeaderMap) -> String {
    headers
        .get("x-bss-actor")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
        .unwrap_or_else(|| "anonymous".to_string())
}

/// Parse an ISO-8601 datetime (accepts `Z` or `+00:00`) to UTC.
fn parse_dt(s: &str) -> Result<DateTime<Utc>, ApiError> {
    DateTime::parse_from_rfc3339(s)
        .map(|dt| dt.with_timezone(&Utc))
        .map_err(|e| ApiError::NotFound(format!("invalid datetime '{s}': {e}")))
}

fn opt_dt(s: Option<&str>) -> Result<Option<DateTime<Utc>>, ApiError> {
    match s {
        Some(v) if !v.is_empty() => parse_dt(v).map(Some),
        _ => Ok(None),
    }
}

/// Deserialize a `Decimal` from either a JSON number or string (Pydantic accepts
/// both for a `Decimal` field).
fn de_decimal<'de, D>(de: D) -> Result<Decimal, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::de::Error;
    use std::str::FromStr;
    let v = Value::deserialize(de)?;
    match v {
        Value::String(s) => Decimal::from_str(&s).map_err(D::Error::custom),
        Value::Number(n) => Decimal::from_str(&n.to_string()).map_err(D::Error::custom),
        other => Err(D::Error::custom(format!(
            "expected number/string for decimal, got {other}"
        ))),
    }
}
