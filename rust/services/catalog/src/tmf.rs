//! TMF620 / VAS response mapping — port of `schemas.tmf620` + `schemas.vas`.
//!
//! Builds the exact wire JSON the live catalog emits (camelCase keys, `@type`
//! discriminators). Two datetime seams to keep straight:
//! - **response bodies** render datetimes with a trailing `Z` (Pydantic v2's
//!   default), fraction omitted when the microsecond is zero — [`tmf_datetime`];
//! - money renders `taxIncludedAmount.value` as a JSON **float** (`float(amount)`)
//!   while the currency lives in `.unit`.
//!
//! (Datetimes embedded in *policy messages* use the `+00:00` `bss_clock::isoformat`
//! seam instead — see `promo_service` / the no-active-price 422.)

use chrono::{DateTime, Utc};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde_json::{json, Value};

use crate::repo::{OfferingFull, PriceRow, SpecRow, VasRow};

pub const OFFERING_PATH: &str = "/tmf-api/productCatalogManagement/v4/productOffering";
pub const OFFERING_PRICE_PATH: &str = "/tmf-api/productCatalogManagement/v4/productOfferingPrice";
pub const SPEC_PATH: &str = "/tmf-api/productCatalogManagement/v4/productSpecification";

/// Pydantic-v2 datetime serialization: RFC3339 with `Z`, microseconds only when
/// non-zero. Matches the live wire (`2026-04-01T00:00:00Z`).
pub fn tmf_datetime(dt: DateTime<Utc>) -> String {
    if dt.timestamp_subsec_micros() == 0 {
        dt.format("%Y-%m-%dT%H:%M:%SZ").to_string()
    } else {
        dt.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
    }
}

/// `float(amount)` — the wire renders money value as a JSON number.
fn money_value(amount: Decimal) -> Value {
    json!(amount.to_f64().unwrap_or(0.0))
}

/// A `TimePeriod` object, or `null` when both bounds are absent. When present,
/// both keys appear (one may be null) — matching Pydantic's inclusion of `None`.
fn valid_for(from: Option<DateTime<Utc>>, to: Option<DateTime<Utc>>) -> Value {
    if from.is_none() && to.is_none() {
        return Value::Null;
    }
    json!({
        "startDateTime": from.map(tmf_datetime),
        "endDateTime": to.map(tmf_datetime),
    })
}

pub fn to_tmf620_price(p: &PriceRow) -> Value {
    json!({
        "id": p.id,
        "priceType": p.price_type,
        "recurringChargePeriodLength": p.recurring_period_length,
        "recurringChargePeriodType": p.recurring_period_type,
        "price": {
            "taxIncludedAmount": { "value": money_value(p.amount), "unit": p.currency }
        },
        "validFor": valid_for(p.valid_from, p.valid_to),
        "@type": "ProductOfferingPrice",
    })
}

pub fn to_tmf620_offering(f: &OfferingFull) -> Value {
    let o = &f.offering;
    let spec_ref = f.spec.as_ref().map(|s| {
        json!({
            "id": s.id,
            "href": format!("{SPEC_PATH}/{}", s.id),
            "name": s.name,
            "@type": "ProductSpecificationRef",
        })
    });
    let prices: Vec<Value> = f.prices.iter().map(to_tmf620_price).collect();
    let allowances: Vec<Value> = f
        .allowances
        .iter()
        .map(|a| json!({ "allowanceType": a.allowance_type, "quantity": a.quantity, "unit": a.unit }))
        .collect();
    json!({
        "id": o.id,
        "href": format!("{OFFERING_PATH}/{}", o.id),
        "name": o.name,
        "isBundle": o.is_bundle,
        "isSellable": o.is_sellable,
        "lifecycleStatus": o.lifecycle_status,
        "validFor": valid_for(o.valid_from, o.valid_to),
        "productSpecification": spec_ref.unwrap_or(Value::Null),
        "productOfferingPrice": prices,
        "bundleAllowance": allowances,
        "@type": "ProductOffering",
    })
}

pub fn to_tmf620_spec(s: &SpecRow) -> Value {
    json!({
        "id": s.id,
        "href": format!("{SPEC_PATH}/{}", s.id),
        "name": s.name,
        "description": s.description,
        "brand": s.brand,
        "lifecycleStatus": s.lifecycle_status,
        "@type": "ProductSpecification",
    })
}

pub fn to_vas_offering(v: &VasRow) -> Value {
    json!({
        "id": v.id,
        "name": v.name,
        "priceAmount": money_value(v.price_amount),
        "currency": v.currency,
        "allowanceType": v.allowance_type,
        "allowanceQuantity": v.allowance_quantity,
        "allowanceUnit": v.allowance_unit,
        "expiryHours": v.expiry_hours,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn datetime_omits_zero_fraction_and_uses_z() {
        let dt = Utc.with_ymd_and_hms(2026, 4, 1, 0, 0, 0).unwrap();
        assert_eq!(tmf_datetime(dt), "2026-04-01T00:00:00Z");
    }

    #[test]
    fn datetime_keeps_micros_when_present() {
        let dt = Utc
            .with_ymd_and_hms(2026, 2, 10, 12, 30, 15)
            .unwrap()
            .with_timezone(&Utc)
            + chrono::Duration::microseconds(123456);
        assert_eq!(tmf_datetime(dt), "2026-02-10T12:30:15.123456Z");
    }

    #[test]
    fn money_value_is_float() {
        // 25.00 → JSON 25.0 (a number, not a string).
        assert_eq!(money_value(Decimal::new(2500, 2)), json!(25.0));
        assert!(money_value(Decimal::new(2500, 2)).is_number());
    }

    #[test]
    fn valid_for_null_when_both_absent() {
        assert_eq!(valid_for(None, None), Value::Null);
    }

    #[test]
    fn valid_for_includes_both_keys_when_one_present() {
        let to = Utc.with_ymd_and_hms(2026, 4, 1, 0, 0, 0).unwrap();
        let vf = valid_for(None, Some(to));
        assert_eq!(vf["startDateTime"], Value::Null);
        assert_eq!(vf["endDateTime"], json!("2026-04-01T00:00:00Z"));
    }
}
