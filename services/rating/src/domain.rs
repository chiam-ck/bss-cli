//! Pure rating logic — no DB, no HTTP, no side effects.
//!
//! Port of `app.domain.rating` + the consumer's pure decision branch (the
//! roaming routing that `app.events.consumer._handle_usage_recorded` does around
//! `rate_usage`). Doctrine: bundled prepaid only — `charge_amount` is always `0`,
//! `consumed_quantity == quantity` (no tiering, no TOD, no per-unit charging).
//!
//! Splitting the consumer's branch into [`decide_usage_outcome`] (pure) keeps the
//! full event-shape decision unit-testable in CI, exactly like the Python
//! `test_rating_event_consumer.py` asserts on the published routing-key + payload.
//! The I/O glue (fetch tariff, stage audit row, publish) lives in `consumer.rs`
//! and is validated against live infra.

use serde_json::{json, Value};

/// Raised when a usage event cannot be rated against the given tariff.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RatingError(pub String);

impl std::fmt::Display for RatingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for RatingError {}

/// Subset of a UsageEvent needed for rating (port of `UsageInput`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsageInput {
    pub usage_event_id: String,
    pub subscription_id: String,
    pub msisdn: String,
    pub event_type: String,
    pub quantity: i64,
    pub unit: String,
}

/// Output of [`rate_usage`] — the decrement instruction for Subscription.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RatingResult {
    pub usage_event_id: String,
    pub subscription_id: String,
    pub allowance_type: String,
    pub consumed_quantity: i64,
    pub unit: String,
    /// Always `"0"` for bundled prepaid v0.1 (surfaced as a string for JSON stability).
    pub charge_amount: String,
    pub currency: String,
}

/// `event_type → allowance_type`. Both `voice` and `voice_minutes` map to the
/// voice allowance (legacy/alias). `None` for an unmapped event type.
fn event_type_to_allowance(event_type: &str) -> Option<&'static str> {
    match event_type {
        "data" => Some("data"),
        "voice" | "voice_minutes" => Some("voice"),
        "sms" => Some("sms"),
        _ => None,
    }
}

/// `allowance_type → canonical unit`. `None` for a non-canonical allowance.
fn allowance_unit(allowance_type: &str) -> Option<&'static str> {
    match allowance_type {
        "data" => Some("mb"),
        "voice" => Some("minutes"),
        "sms" => Some("count"),
        _ => None,
    }
}

/// Pure function. Returns the decrement instruction for Subscription, or a
/// [`RatingError`] when the event type is unmapped, the tariff lacks the
/// allowance, or the usage unit doesn't match the allowance unit.
pub fn rate_usage(usage: &UsageInput, tariff: &Value) -> Result<RatingResult, RatingError> {
    let allowance_type = event_type_to_allowance(&usage.event_type).ok_or_else(|| {
        RatingError(format!(
            "No allowance mapping for event_type '{}'",
            usage.event_type
        ))
    })?;

    let allowances = tariff
        .get("bundleAllowance")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let matching = allowances
        .iter()
        .find(|a| a.get("allowanceType").and_then(Value::as_str) == Some(allowance_type));
    if matching.is_none() {
        return Err(RatingError(format!(
            "Tariff '{}' has no '{}' allowance",
            tariff_id_display(tariff),
            allowance_type
        )));
    }

    // allowance_type ∈ {data,voice,sms} here, so `allowance_unit` always returns
    // Some; the `?` keeps clippy happy without an unwrap.
    let expected_unit = allowance_unit(allowance_type)
        .ok_or_else(|| RatingError(format!("No canonical unit for '{allowance_type}'")))?;
    if usage.unit != expected_unit {
        return Err(RatingError(format!(
            "Usage unit '{}' does not match allowance unit '{}' for {}",
            usage.unit, expected_unit, allowance_type
        )));
    }

    let currency = currency_of(tariff);

    Ok(RatingResult {
        usage_event_id: usage.usage_event_id.clone(),
        subscription_id: usage.subscription_id.clone(),
        allowance_type: allowance_type.to_string(),
        consumed_quantity: usage.quantity,
        unit: usage.unit.clone(),
        charge_amount: "0".to_string(),
        currency,
    })
}

/// First `productOfferingPrice[].price.taxIncludedAmount.unit` (the currency
/// code), defaulting to `"SGD"` — mirrors the Python scan.
fn currency_of(tariff: &Value) -> String {
    let entries = tariff
        .get("productOfferingPrice")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    for p in &entries {
        let unit = p
            .get("price")
            .and_then(|price| price.get("taxIncludedAmount"))
            .and_then(|tia| tia.get("unit"))
            .and_then(Value::as_str);
        if let Some(u) = unit {
            if !u.is_empty() {
                return u.to_string();
            }
        }
    }
    "SGD".to_string()
}

/// Render `tariff["id"]` the way the Python f-string would (missing → `"None"`).
fn tariff_id_display(tariff: &Value) -> String {
    match tariff.get("id") {
        Some(Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
        None => "None".to_string(),
    }
}

/// The single event a rated usage produces: routing key + audit aggregate +
/// payload. Both the `usage.rated` and `usage.rejected` branches yield one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsageOutcome {
    pub event_type: &'static str,
    pub aggregate_type: &'static str,
    pub aggregate_id: String,
    pub payload: Value,
}

/// Read `offeringId` off a `usage.recorded` body, raising the exact Python
/// message when absent (checked *before* any catalog fetch).
pub fn require_offering_id(body: &Value) -> Result<String, RatingError> {
    match body.get("offeringId").and_then(Value::as_str) {
        Some(s) if !s.is_empty() => Ok(s.to_string()),
        _ => Err(RatingError(format!(
            "usage.recorded payload missing offeringId (usage_event_id={})",
            body.get("usageEventId")
                .and_then(Value::as_str)
                .unwrap_or("None")
        ))),
    }
}

/// Pure decision over a `usage.recorded` body + its tariff: which event to emit
/// and with what payload. Ports the roaming routing (v0.17) the consumer does
/// around `rate_usage`:
/// - roaming + `data` + no `data_roaming` allowance → `usage.rejected`;
/// - roaming + `data` + has `data_roaming` allowance → allowance becomes `data_roaming`;
/// - otherwise → `usage.rated` on the plain allowance.
pub fn decide_usage_outcome(
    body: &Value,
    tariff: &Value,
    offering_id: &str,
) -> Result<UsageOutcome, RatingError> {
    let usage = UsageInput {
        usage_event_id: str_field(body, "usageEventId")?,
        subscription_id: str_field(body, "subscriptionId")?,
        msisdn: str_field(body, "msisdn")?,
        event_type: str_field(body, "eventType")?,
        quantity: int_field(body, "quantity")?,
        unit: str_field(body, "unit")?,
    };
    let result = rate_usage(&usage, tariff)?;

    let mut allowance_type = result.allowance_type.clone();
    if truthy(body.get("roamingIndicator")) && allowance_type == "data" {
        let has_roaming = tariff
            .get("bundleAllowance")
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .any(|a| a.get("allowanceType").and_then(Value::as_str) == Some("data_roaming"))
            })
            .unwrap_or(false);
        if !has_roaming {
            return Ok(UsageOutcome {
                event_type: "usage.rejected",
                aggregate_type: "usage",
                aggregate_id: result.usage_event_id.clone(),
                payload: json!({
                    "usageEventId": result.usage_event_id,
                    "subscriptionId": result.subscription_id,
                    "msisdn": usage.msisdn,
                    "eventType": usage.event_type,
                    "reason": "rating.no_roaming_allowance",
                    "offeringId": offering_id,
                }),
            });
        }
        allowance_type = "data_roaming".to_string();
    }

    Ok(UsageOutcome {
        event_type: "usage.rated",
        aggregate_type: "usage",
        aggregate_id: result.usage_event_id.clone(),
        payload: json!({
            "usageEventId": result.usage_event_id,
            "subscriptionId": result.subscription_id,
            "allowanceType": allowance_type,
            "consumedQuantity": result.consumed_quantity,
            "unit": result.unit,
            "chargeAmount": result.charge_amount,
            "currency": result.currency,
            "offeringId": offering_id,
        }),
    })
}

fn str_field(body: &Value, key: &str) -> Result<String, RatingError> {
    body.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| RatingError(format!("usage.recorded payload missing '{key}'")))
}

/// Mirror `int(body[key])` — accept a JSON number or a numeric string.
fn int_field(body: &Value, key: &str) -> Result<i64, RatingError> {
    match body.get(key) {
        Some(Value::Number(n)) => n
            .as_i64()
            .ok_or_else(|| RatingError(format!("'{key}' is not an integer"))),
        Some(Value::String(s)) => s
            .parse::<i64>()
            .map_err(|_| RatingError(format!("'{key}' is not an integer: '{s}'"))),
        _ => Err(RatingError(format!(
            "usage.recorded payload missing '{key}'"
        ))),
    }
}

/// Python `bool(body.get(key, False))` truthiness over a JSON value.
fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().map(|x| x != 0.0).unwrap_or(false),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use serde_json::json;

    fn plan_m() -> Value {
        json!({
            "id": "PLAN_M",
            "name": "Standard",
            "productOfferingPrice": [
                {"priceType": "recurring", "price": {"taxIncludedAmount": {"value": "25.00", "unit": "SGD"}}}
            ],
            "bundleAllowance": [
                {"allowanceType": "data", "quantity": 30720, "unit": "mb"},
                {"allowanceType": "voice", "quantity": -1, "unit": "minutes"},
                {"allowanceType": "sms", "quantity": -1, "unit": "count"}
            ]
        })
    }

    fn plan_data_only() -> Value {
        json!({
            "id": "PLAN_DATA_ONLY",
            "bundleAllowance": [{"allowanceType": "data", "quantity": 5120, "unit": "mb"}]
        })
    }

    fn usage(event_type: &str, quantity: i64, unit: &str) -> UsageInput {
        UsageInput {
            usage_event_id: "UE-000001".into(),
            subscription_id: "SUB-0001".into(),
            msisdn: "90000042".into(),
            event_type: event_type.into(),
            quantity,
            unit: unit.into(),
        }
    }

    // ── rate_usage matrix (ports test_rating_pure_function.py) ──────────────

    #[test]
    fn plan_m_happy_path() {
        for (et, qty, unit, expected) in [
            ("data", 100, "mb", "data"),
            ("data", 1, "mb", "data"),
            ("data", 1_000_000, "mb", "data"),
            ("voice", 5, "minutes", "voice"),
            ("voice_minutes", 10, "minutes", "voice"),
            ("sms", 1, "count", "sms"),
            ("sms", 50, "count", "sms"),
        ] {
            let r = rate_usage(&usage(et, qty, unit), &plan_m()).unwrap();
            assert_eq!(r.allowance_type, expected);
            assert_eq!(r.consumed_quantity, qty);
            assert_eq!(r.unit, unit);
            assert_eq!(r.charge_amount, "0");
            assert_eq!(r.currency, "SGD");
            assert_eq!(r.subscription_id, "SUB-0001");
            assert_eq!(r.usage_event_id, "UE-000001");
        }
    }

    #[test]
    fn no_matching_allowance_raises() {
        let e = rate_usage(&usage("voice_minutes", 1, "minutes"), &plan_data_only()).unwrap_err();
        assert!(e.0.contains("no 'voice' allowance"), "{}", e.0);
        let e = rate_usage(&usage("sms", 1, "count"), &plan_data_only()).unwrap_err();
        assert!(e.0.contains("no 'sms' allowance"), "{}", e.0);
    }

    #[test]
    fn unknown_event_type_raises() {
        let e = rate_usage(&usage("video", 1, "mb"), &plan_m()).unwrap_err();
        assert!(e.0.contains("No allowance mapping"), "{}", e.0);
    }

    #[test]
    fn unit_mismatch_raises() {
        let e = rate_usage(&usage("data", 1, "gb"), &plan_m()).unwrap_err();
        assert!(e.0.contains("does not match allowance unit"), "{}", e.0);
        let e = rate_usage(&usage("voice", 1, "seconds"), &plan_m()).unwrap_err();
        assert!(e.0.contains("does not match allowance unit"), "{}", e.0);
    }

    #[test]
    fn empty_or_missing_bundle_allowance_raises() {
        let e = rate_usage(
            &usage("data", 100, "mb"),
            &json!({"id": "PLAN_X", "bundleAllowance": []}),
        )
        .unwrap_err();
        assert!(e.0.contains("no 'data' allowance"), "{}", e.0);
        let e = rate_usage(&usage("data", 100, "mb"), &json!({"id": "PLAN_X"})).unwrap_err();
        assert!(e.0.contains("no 'data' allowance"), "{}", e.0);
    }

    #[test]
    fn currency_defaults_to_sgd_when_absent() {
        let tariff = json!({"id": "PLAN_X", "bundleAllowance": [
            {"allowanceType": "data", "quantity": 1024, "unit": "mb"}]});
        let r = rate_usage(&usage("data", 100, "mb"), &tariff).unwrap();
        assert_eq!(r.currency, "SGD");
    }

    // ── decide_usage_outcome (ports test_rating_event_consumer.py payloads) ──

    fn body(event_type: &str, quantity: i64, roaming: Option<bool>) -> Value {
        let mut b = json!({
            "usageEventId": "UE-TEST-1",
            "subscriptionId": "SUB-0001",
            "msisdn": "90000042",
            "eventType": event_type,
            "quantity": quantity,
            "unit": "mb",
            "offeringId": "PLAN_M",
        });
        if let Some(r) = roaming {
            b["roamingIndicator"] = json!(r);
        }
        b
    }

    fn plan_m_with_roaming() -> Value {
        json!({
            "id": "PLAN_M",
            "bundleAllowance": [
                {"allowanceType": "data", "quantity": 30720, "unit": "mb"},
                {"allowanceType": "voice", "quantity": -1, "unit": "minutes"},
                {"allowanceType": "data_roaming", "quantity": 500, "unit": "mb"}
            ],
            "productOfferingPrice": [
                {"price": {"taxIncludedAmount": {"value": "25.00", "unit": "SGD"}}}
            ]
        })
    }

    #[test]
    fn emits_usage_rated() {
        let o = decide_usage_outcome(&body("data", 1000, None), &plan_m(), "PLAN_M").unwrap();
        assert_eq!(o.event_type, "usage.rated");
        assert_eq!(o.payload["subscriptionId"], "SUB-0001");
        assert_eq!(o.payload["allowanceType"], "data");
        assert_eq!(o.payload["consumedQuantity"], 1000);
        assert_eq!(o.payload["chargeAmount"], "0");
        assert_eq!(o.payload["offeringId"], "PLAN_M");
    }

    #[test]
    fn missing_offering_id_raises_before_fetch() {
        let mut b = body("data", 1, None);
        b.as_object_mut().unwrap().remove("offeringId");
        let e = require_offering_id(&b).unwrap_err();
        assert!(e.0.contains("missing offeringId"), "{}", e.0);
    }

    #[test]
    fn tariff_without_allowance_raises() {
        let e = decide_usage_outcome(
            &body("data", 1, None),
            &json!({"id": "PLAN_X", "bundleAllowance": []}),
            "PLAN_X",
        )
        .unwrap_err();
        assert!(e.0.contains("no 'data' allowance"), "{}", e.0);
    }

    #[test]
    fn roaming_indicator_overrides_to_data_roaming() {
        let o = decide_usage_outcome(
            &body("data", 100, Some(true)),
            &plan_m_with_roaming(),
            "PLAN_M",
        )
        .unwrap();
        assert_eq!(o.event_type, "usage.rated");
        assert_eq!(o.payload["allowanceType"], "data_roaming");
        assert_eq!(o.payload["consumedQuantity"], 100);
    }

    #[test]
    fn roaming_without_allowance_emits_rejected() {
        // plan_m has no data_roaming allowance
        let o = decide_usage_outcome(&body("data", 100, Some(true)), &plan_m(), "PLAN_M").unwrap();
        assert_eq!(o.event_type, "usage.rejected");
        assert_eq!(o.payload["reason"], "rating.no_roaming_allowance");
    }

    #[test]
    fn no_roaming_indicator_routes_as_data() {
        // roamingIndicator absent — pre-v0.17 caller shape, routes to `data`
        let o = decide_usage_outcome(&body("data", 100, None), &plan_m_with_roaming(), "PLAN_M")
            .unwrap();
        assert_eq!(o.event_type, "usage.rated");
        assert_eq!(o.payload["allowanceType"], "data");
    }
}
