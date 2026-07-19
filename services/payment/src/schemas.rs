//! TMF676 request/response shapes — port of `app.schemas.tmf.*`.
//!
//! Response builders emit the exact live wire: camelCase keys, `@type`
//! discriminators, **nulls present** (not omitted), datetimes with a trailing `Z`
//! (micros only when non-zero — Pydantic v2), and `amount` as a 2dp **string**
//! (`"25.00"`; the scale is preserved by the `amount::text` read).

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::repo::{PaymentAttemptRow, PaymentMethodRow};

pub const PAYMENT_PATH: &str = "/tmf-api/paymentManagement/v4/payment";
pub const PAYMENT_METHOD_PATH: &str = "/tmf-api/paymentMethodManagement/v4/paymentMethod";

/// Pydantic-v2 datetime serialization: RFC3339 with `Z`, micros only when
/// non-zero. Matches the live wire (`2026-06-03T09:01:00Z`,
/// `2026-07-12T07:10:21.778715Z`).
pub fn tmf_datetime(dt: DateTime<Utc>) -> String {
    if dt.timestamp_subsec_micros() == 0 {
        dt.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        dt.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
    }
}

// ── Request bodies ───────────────────────────────────────────────────

fn de_decimal<'de, D>(d: D) -> Result<Decimal, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::de::Error;
    // Accept a JSON string ("25.00") or number (25.0) — Pydantic's Decimal does.
    match Value::deserialize(d)? {
        Value::String(s) => s.parse::<Decimal>().map_err(D::Error::custom),
        Value::Number(n) => n.to_string().parse::<Decimal>().map_err(D::Error::custom),
        other => Err(D::Error::custom(format!(
            "amount not a number/string: {other}"
        ))),
    }
}

fn default_sgd() -> String {
    "SGD".to_string()
}

fn default_card() -> String {
    "card".to_string()
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PaymentChargeRequest {
    #[serde(alias = "customer_id")]
    pub customer_id: String,
    #[serde(alias = "payment_method_id")]
    pub payment_method_id: String,
    #[serde(deserialize_with = "de_decimal")]
    pub amount: Decimal,
    #[serde(default = "default_sgd")]
    pub currency: String,
    pub purpose: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CardSummary {
    pub brand: String,
    pub last4: String,
    #[serde(alias = "exp_month")]
    pub exp_month: i32,
    #[serde(alias = "exp_year")]
    pub exp_year: i32,
    #[serde(default)]
    pub country: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PaymentMethodCreateRequest {
    #[serde(alias = "customer_id")]
    pub customer_id: String,
    #[serde(default = "default_card", rename = "type")]
    pub type_: String,
    #[serde(alias = "tokenization_provider")]
    pub tokenization_provider: String,
    #[serde(alias = "provider_token")]
    pub provider_token: String,
    #[serde(alias = "card_summary")]
    pub card_summary: CardSummary,
}

// ── Response builders ────────────────────────────────────────────────

pub fn to_payment_attempt_response(a: &PaymentAttemptRow) -> Value {
    json!({
        "id": a.id,
        "href": format!("{PAYMENT_PATH}/{}", a.id),
        "customerId": a.customer_id,
        "paymentMethodId": a.payment_method_id,
        "amount": a.amount.to_string(),
        "currency": a.currency,
        "purpose": a.purpose,
        "status": a.status,
        "gatewayRef": a.gateway_ref,
        "declineReason": a.decline_reason,
        "attemptedAt": tmf_datetime(a.attempted_at),
        "@type": "Payment",
    })
}

/// `tokenization_provider` is echoed only on the POST response (the create call
/// passes `body.tokenizationProvider`); GET/list/delete pass `None` → `null`,
/// matching the oracle's `to_payment_method_response(pm)` default.
pub fn to_payment_method_response(
    m: &PaymentMethodRow,
    tokenization_provider: Option<&str>,
) -> Value {
    json!({
        "id": m.id,
        "href": format!("{PAYMENT_METHOD_PATH}/{}", m.id),
        "customerId": m.customer_id,
        "type": m.type_,
        "tokenizationProvider": tokenization_provider,
        "providerToken": m.token,
        "cardSummary": {
            "brand": m.brand.clone().unwrap_or_else(|| "unknown".to_string()),
            "last4": m.last4,
            "expMonth": m.exp_month,
            "expYear": m.exp_year,
            "country": Value::Null,
        },
        "isDefault": m.is_default,
        "status": m.status,
        "createdAt": tmf_datetime(m.created_at),
        "@type": "PaymentMethod",
    })
}
