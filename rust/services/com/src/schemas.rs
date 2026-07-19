//! TMF622 ProductOrder response mapping — port of `app.schemas.order`.
//!
//! camelCase keys, `@type` discriminator. Datetimes render with a trailing `Z`
//! (Pydantic v2), fraction omitted when zero. `priceAmount`/`discountValue` are
//! Pydantic `Decimal` fields → rendered as **strings** (`"25.00"`), unlike the
//! catalog Money float.

use chrono::{DateTime, Utc};
use serde_json::{json, Value};

use crate::repo::{ItemRow, OrderFull, OrderRow};

pub const ORDER_PATH: &str = "/tmf-api/productOrderingManagement/v4/productOrder";

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

fn to_order_item(item: &ItemRow) -> Value {
    json!({
        "id": item.id,
        "action": item.action,
        "offeringId": item.offering_id,
        "state": item.state,
        "targetSubscriptionId": item.target_subscription_id,
        "priceAmount": item.price_amount.map(|d| d.to_string()),
        "priceCurrency": item.price_currency,
        "priceOfferingPriceId": item.price_offering_price_id,
        "discountCode": item.discount_code,
        "promoOfferDefinitionId": item.promo_offer_definition_id,
        "discountType": item.discount_type,
        "discountValue": item.discount_value.map(|d| d.to_string()),
        "discountPeriodsTotal": item.discount_periods_total,
        "promoOfferId": item.promo_offer_id,
    })
}

fn order_base(order: &OrderRow) -> Value {
    json!({
        "id": order.id,
        "href": format!("{ORDER_PATH}/{}", order.id),
        "customerId": order.customer_id,
        "state": order.state,
        "orderDate": dt(order.order_date),
        "requestedCompletionDate": dt(order.requested_completion_date),
        "completedDate": dt(order.completed_date),
        "msisdnPreference": order.msisdn_preference,
        "notes": order.notes,
        "@type": "ProductOrder",
    })
}

pub fn to_product_order(full: &OrderFull) -> Value {
    let mut v = order_base(&full.order);
    v["items"] = Value::Array(full.items.iter().map(to_order_item).collect());
    v
}
