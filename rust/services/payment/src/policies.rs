//! Policy layer — port of `app.policies.payment` (4 charge rules) and
//! `app.policies.payment_method` (5 method rules). Each returns
//! `Result<_, PolicyViolation>`; the router maps that onto the frozen 422.

use bss_clients::{ClientError, CrmClient};
use bss_db::PolicyViolation;
use rust_decimal::Decimal;
use serde_json::{json, Value};

use crate::repo::PaymentMethodRow;

pub const MAX_METHODS_PER_CUSTOMER: i64 = 5;

// ── charge policies ──────────────────────────────────────────────────

pub fn check_method_active(method: &PaymentMethodRow) -> Result<(), PolicyViolation> {
    if method.status != "active" {
        return Err(PolicyViolation::with_context(
            "payment.charge.method_active",
            format!(
                "Payment method {} status is '{}', must be active",
                method.id, method.status
            ),
            json!({ "payment_method_id": method.id, "status": method.status }),
        ));
    }
    Ok(())
}

pub fn check_positive_amount(amount: &Decimal) -> Result<(), PolicyViolation> {
    if *amount <= Decimal::ZERO {
        return Err(PolicyViolation::with_context(
            "payment.charge.positive_amount",
            format!("Charge amount must be positive, got {amount}"),
            json!({ "amount": amount.to_string() }),
        ));
    }
    Ok(())
}

pub fn check_customer_matches_method(
    customer_id: &str,
    method: &PaymentMethodRow,
) -> Result<(), PolicyViolation> {
    if method.customer_id != customer_id {
        return Err(PolicyViolation::with_context(
            "payment.charge.customer_matches_method",
            format!(
                "Payment method {} belongs to {}, not {}",
                method.id, method.customer_id, customer_id
            ),
            json!({
                "requested_customer_id": customer_id,
                "method_customer_id": method.customer_id,
                "payment_method_id": method.id,
            }),
        ));
    }
    Ok(())
}

/// v0.16 lazy-fail cutover guard — the row's `token_provider` must match the
/// active adapter. Unknown adapter names (test doubles) don't trip. Port of
/// `check_token_provider_matches_active` (`_ADAPTER_EXPECTS`).
pub fn check_token_provider_matches_active(
    method: &PaymentMethodRow,
    adapter_class_name: &str,
) -> Result<(), PolicyViolation> {
    let expected = match adapter_class_name {
        "MockTokenizerAdapter" => "mock",
        "StripeTokenizerAdapter" => "stripe",
        _ => return Ok(()),
    };
    let actual = if method.token_provider.is_empty() {
        "mock"
    } else {
        method.token_provider.as_str()
    };
    if actual != expected {
        return Err(PolicyViolation::with_context(
            "payment.charge.token_provider_matches_active",
            format!(
                "Payment method {} has token_provider='{actual}', but the active tokenizer is \
                 {adapter_class_name} (expects token_provider='{expected}'). Customer must \
                 re-add their card; see `docs/runbooks/stripe-cutover.md`.",
                method.id
            ),
            json!({
                "payment_method_id": method.id,
                "row_token_provider": actual,
                "active_adapter": adapter_class_name,
                "expected_token_provider": expected,
            }),
        ));
    }
    Ok(())
}

// ── payment-method policies ──────────────────────────────────────────

/// Cross-service existence check. Returns the CRM customer payload for the
/// downstream `active_or_pending` check. A 404 → `customer_exists` violation.
pub async fn check_customer_exists(
    customer_id: &str,
    crm: &CrmClient,
) -> Result<Value, PolicyViolation> {
    match crm.get_customer(customer_id).await {
        Ok(v) => Ok(v),
        Err(ClientError::NotFound(_)) => Err(PolicyViolation::with_context(
            "payment_method.add.customer_exists",
            format!("Customer {customer_id} does not exist"),
            json!({ "customer_id": customer_id }),
        )),
        // Any other transport/server error is not a policy outcome — surface it
        // so the middleware renders a 500 (Python lets ServerError bubble).
        Err(e) => Err(PolicyViolation::with_context(
            "payment_method.add.customer_lookup_failed",
            format!("Customer lookup failed: {e}"),
            json!({ "customer_id": customer_id }),
        )),
    }
}

pub fn check_customer_active_or_pending(customer: &Value) -> Result<(), PolicyViolation> {
    let status = customer.get("status").and_then(Value::as_str).unwrap_or("");
    if status != "active" && status != "pending" {
        return Err(PolicyViolation::with_context(
            "payment_method.add.customer_active_or_pending",
            format!("Customer status is '{status}', must be active or pending"),
            json!({ "status": status }),
        ));
    }
    Ok(())
}

pub fn check_card_not_expired(exp_month: i32, exp_year: i32) -> Result<(), PolicyViolation> {
    let now = bss_clock::now();
    let (year, month) = (
        chrono::Datelike::year(&now),
        chrono::Datelike::month(&now) as i32,
    );
    if exp_year < year || (exp_year == year && exp_month < month) {
        return Err(PolicyViolation::with_context(
            "payment_method.add.card_not_expired",
            format!("Card expired: {exp_month:02}/{exp_year}"),
            json!({ "exp_month": exp_month, "exp_year": exp_year }),
        ));
    }
    Ok(())
}

pub fn check_at_most_n_methods(
    customer_id: &str,
    active_count: i64,
) -> Result<(), PolicyViolation> {
    if active_count >= MAX_METHODS_PER_CUSTOMER {
        return Err(PolicyViolation::with_context(
            "payment_method.add.at_most_n_methods",
            format!(
                "Customer {customer_id} already has {active_count} active payment methods \
                 (max {MAX_METHODS_PER_CUSTOMER})"
            ),
            json!({
                "customer_id": customer_id,
                "current_count": active_count,
                "max": MAX_METHODS_PER_CUSTOMER,
            }),
        ));
    }
    Ok(())
}

// check_not_last_if_active_subscription — STUB in the oracle (always allows);
// intentionally omitted here (a no-op fn would only add noise). Ports when the
// SubscriptionClient cross-check is wired (oracle's Phase 6 note).
