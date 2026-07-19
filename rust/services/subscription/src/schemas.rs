//! Request bodies + TMF-ish response mapping — port of `app.schemas.subscription`.
//!
//! camelCase keys with snake_case aliases (Pydantic `populate_by_name`). Datetimes
//! render RFC3339 with `Z`, micros only when nonzero (Pydantic v2 / speedate).
//! `priceAmount` / `discountValue` / `effectiveAmount` are Pydantic `Decimal` →
//! **strings** (`"25.00"`). The response `balances` array preserves the read order
//! (insertion order — matches the oracle's un-ordered selectinload).

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::money::apply_discount;
use crate::repo::{BundleBalanceRow, SubscriptionFull, SubscriptionRow};

pub const SUBSCRIPTION_PATH: &str = "/subscription-api/v1/subscription";

/// Pydantic-v2 datetime serialization: RFC3339 with `Z`, micros only when nonzero.
pub fn tmf_datetime(dt: DateTime<Utc>) -> String {
    if dt.timestamp_subsec_micros() == 0 {
        dt.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        dt.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
    }
}

fn dt(v: Option<DateTime<Utc>>) -> Value {
    v.map(tmf_datetime).map(Value::from).unwrap_or(Value::Null)
}

fn dec(v: Decimal) -> Value {
    Value::from(v.to_string())
}

fn opt_dec(v: Option<Decimal>) -> Value {
    v.map(|d| Value::from(d.to_string())).unwrap_or(Value::Null)
}

// ── request bodies ──────────────────────────────────────────────────────────

fn de_decimal<'de, D>(d: D) -> Result<Decimal, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::de::Error;
    match Value::deserialize(d)? {
        Value::String(s) => s.parse::<Decimal>().map_err(D::Error::custom),
        Value::Number(n) => n.to_string().parse::<Decimal>().map_err(D::Error::custom),
        other => Err(D::Error::custom(format!(
            "amount not a number/string: {other}"
        ))),
    }
}

fn de_opt_decimal<'de, D>(d: D) -> Result<Option<Decimal>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::de::Error;
    match Option::<Value>::deserialize(d)? {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => s.parse::<Decimal>().map(Some).map_err(D::Error::custom),
        Some(Value::Number(n)) => n
            .to_string()
            .parse::<Decimal>()
            .map(Some)
            .map_err(D::Error::custom),
        Some(other) => Err(D::Error::custom(format!(
            "discount not a number/string: {other}"
        ))),
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PriceSnapshot {
    #[serde(alias = "price_amount", deserialize_with = "de_decimal")]
    pub price_amount: Decimal,
    #[serde(alias = "price_currency")]
    pub price_currency: String,
    #[serde(alias = "price_offering_price_id")]
    pub price_offering_price_id: String,
    #[serde(alias = "discount_type", default)]
    pub discount_type: Option<String>,
    #[serde(alias = "discount_value", default, deserialize_with = "de_opt_decimal")]
    pub discount_value: Option<Decimal>,
    #[serde(alias = "discount_periods_total", default)]
    pub discount_periods_total: Option<i64>,
    #[serde(alias = "promo_code", default)]
    pub promo_code: Option<String>,
    #[serde(alias = "promo_offer_definition_id", default)]
    pub promo_offer_definition_id: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SubscriptionCreateRequest {
    #[serde(alias = "customer_id")]
    pub customer_id: String,
    #[serde(alias = "offering_id")]
    pub offering_id: String,
    pub msisdn: String,
    pub iccid: String,
    #[serde(alias = "payment_method_id")]
    pub payment_method_id: String,
    #[serde(alias = "price_snapshot", default)]
    pub price_snapshot: Option<PriceSnapshot>,
    #[serde(alias = "commercial_order_id", default)]
    pub commercial_order_id: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct VasPurchaseRequest {
    #[serde(alias = "vas_offering_id")]
    pub vas_offering_id: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TerminateRequest {
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(alias = "release_inventory", default = "default_true")]
    pub release_inventory: bool,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SchedulePlanChangeRequest {
    #[serde(alias = "new_offering_id")]
    pub new_offering_id: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MigratePriceRequest {
    #[serde(alias = "offering_id")]
    pub offering_id: String,
    #[serde(alias = "new_price_id")]
    pub new_price_id: String,
    #[serde(alias = "effective_from")]
    pub effective_from: DateTime<Utc>,
    #[serde(alias = "notice_days", default = "default_notice_days")]
    pub notice_days: i64,
    #[serde(alias = "initiated_by")]
    pub initiated_by: String,
}

fn default_notice_days() -> i64 {
    30
}

// ── response builders ───────────────────────────────────────────────────────

pub fn to_balance_response(b: &BundleBalanceRow) -> Value {
    let remaining = if b.total >= 0 {
        b.total - b.consumed
    } else {
        -1
    };
    json!({
        "id": b.id,
        "subscriptionId": b.subscription_id,
        "allowanceType": b.allowance_type,
        "total": b.total,
        "consumed": b.consumed,
        "remaining": remaining,
        "unit": b.unit,
        "periodStart": dt(b.period_start),
        "periodEnd": dt(b.period_end),
    })
}

/// Effective (this-period) charge: discounted while the counter is live (>0 or
/// perpetual -1), else the full base. Mirrors the oracle's `effective_amount`.
fn effective_amount(sub: &SubscriptionRow) -> Decimal {
    match (&sub.discount_type, sub.discount_value) {
        (Some(dtype), Some(dval)) if sub.discount_periods_remaining != 0 => {
            apply_discount(dtype, dval, sub.price_amount).unwrap_or(sub.price_amount)
        }
        _ => sub.price_amount,
    }
}

pub fn to_subscription_response(full: &SubscriptionFull) -> Value {
    let s = &full.sub;
    json!({
        "id": s.id,
        "href": format!("{SUBSCRIPTION_PATH}/{}", s.id),
        "customerId": s.customer_id,
        "offeringId": s.offering_id,
        "msisdn": s.msisdn,
        "iccid": s.iccid,
        "cfsServiceId": s.cfs_service_id,
        "state": s.state,
        "stateReason": s.state_reason,
        "activatedAt": dt(s.activated_at),
        "currentPeriodStart": dt(s.current_period_start),
        "currentPeriodEnd": dt(s.current_period_end),
        "nextRenewalAt": dt(s.next_renewal_at),
        "terminatedAt": dt(s.terminated_at),
        "balances": full.balances.iter().map(to_balance_response).collect::<Vec<_>>(),
        "priceAmount": dec(s.price_amount),
        "priceCurrency": s.price_currency,
        "priceOfferingPriceId": s.price_offering_price_id,
        "pendingOfferingId": s.pending_offering_id,
        "pendingOfferingPriceId": s.pending_offering_price_id,
        "pendingEffectiveAt": dt(s.pending_effective_at),
        "discountType": s.discount_type,
        "discountValue": opt_dec(s.discount_value),
        "discountPeriodsRemaining": s.discount_periods_remaining,
        "effectiveAmount": dec(effective_amount(s)),
        "promoCode": s.promo_code,
        "promoOfferDefinitionId": s.promo_offer_definition_id,
        "atType": "Subscription",
    })
}
