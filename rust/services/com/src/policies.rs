//! Order domain policies over the S2S clients — port of `app.policies.order`'s
//! async checks (the pure FSM lives in `domain`).
//!
//! Each returns the fetched document on success (so callers reuse the read) or a
//! `PolicyViolation`. A non-policy/non-404 client error propagates as `Internal`.

use bss_clients::{CatalogClient, ClientError, CrmClient, PaymentClient, SomClient};
use bss_db::PolicyViolation;
use serde_json::{json, Value};

use crate::error::ApiError;

/// Customer must exist. A 404 → `order.create.customer_not_found`.
pub async fn check_customer_exists(customer_id: &str, crm: &CrmClient) -> Result<Value, ApiError> {
    match crm.get_customer(customer_id).await {
        Ok(v) => Ok(v),
        Err(ClientError::NotFound(_)) => Err(PolicyViolation::with_context(
            "order.create.customer_not_found",
            format!("Customer {customer_id} not found"),
            json!({ "customer_id": customer_id }),
        )
        .into()),
        Err(e) => Err(upstream(e)),
    }
}

/// Offering must exist (read path — no time filter). A 404 →
/// `order.create.offering_not_found`.
async fn check_offering_exists(
    offering_id: &str,
    catalog: &CatalogClient,
) -> Result<Value, ApiError> {
    match catalog.get_offering(offering_id).await {
        Ok(v) => Ok(v),
        Err(ClientError::NotFound(_)) => Err(PolicyViolation::with_context(
            "order.create.offering_not_found",
            format!("Offering {offering_id} not found"),
            json!({ "offering_id": offering_id }),
        )
        .into()),
        Err(e) => Err(upstream(e)),
    }
}

/// Offering must exist AND be sellable now. Catalog's `catalog.price.no_active_row`
/// (a policy 422) surfaces as `policy.offering.not_sellable_now`. Returns the
/// active price document.
pub async fn check_offering_currently_sellable(
    offering_id: &str,
    catalog: &CatalogClient,
) -> Result<Value, ApiError> {
    check_offering_exists(offering_id, catalog).await?;
    match catalog.get_active_price(offering_id).await {
        Ok(price) => Ok(price),
        Err(ClientError::Policy(pv)) => Err(PolicyViolation::with_context(
            "policy.offering.not_sellable_now",
            format!("Offering {offering_id} is not sellable at this time"),
            json!({ "offering_id": offering_id, "underlying": pv.rule }),
        )
        .into()),
        Err(e) => Err(upstream(e)),
    }
}

/// Customer must have at least one payment method (card-on-file). Returns the list.
pub async fn check_customer_has_payment_method(
    customer_id: &str,
    payment: &PaymentClient,
) -> Result<Value, ApiError> {
    let methods = payment.list_methods(customer_id).await.map_err(upstream)?;
    let empty = methods.as_array().map(|a| a.is_empty()).unwrap_or(true);
    if empty {
        return Err(PolicyViolation::with_context(
            "order.create.no_payment_method",
            format!("Customer {customer_id} has no payment method on file"),
            json!({ "customer_id": customer_id }),
        )
        .into());
    }
    Ok(methods)
}

/// If in_progress, cancel only if SOM hasn't started real provisioning.
pub async fn check_cancel_allowed_after_som(
    order_id: &str,
    som: &SomClient,
) -> Result<(), ApiError> {
    let service_orders = som.list_for_order(order_id).await.map_err(upstream)?;
    if let Some(list) = service_orders.as_array() {
        for so in list {
            let state = so.get("state").and_then(Value::as_str);
            // Allowed to cancel only when SO is still "acknowledged" (or absent).
            if !matches!(state, Some("acknowledged") | None) {
                return Err(PolicyViolation::with_context(
                    "order.cancel.forbidden_after_som_started",
                    format!(
                        "Order {order_id} cannot be cancelled — service order {} is in state '{}'",
                        so.get("id").and_then(Value::as_str).unwrap_or(""),
                        state.unwrap_or("")
                    ),
                    json!({
                        "order_id": order_id,
                        "service_order_id": so.get("id"),
                        "service_order_state": so.get("state"),
                    }),
                )
                .into());
            }
        }
    }
    Ok(())
}

fn upstream(e: ClientError) -> ApiError {
    ApiError::Internal(format!("upstream error: {e}"))
}
