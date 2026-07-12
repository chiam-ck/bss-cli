//! Subscription policies — port of `app.policies.*`.
//!
//! Invariants enforced before any write: customer exists + active/pending,
//! MSISDN + eSIM reserved, renew only from active/blocked, plan-change eligibility,
//! roaming-balance gate, VAS-offering existence. On violation a `PolicyViolation`
//! (→ 422) is raised; a genuine upstream error (timeout/5xx) becomes
//! `ApiError::Internal`. Rule namespaces match the oracle byte-for-byte.

use bss_clients::{CatalogClient, ClientError, CrmClient, InventoryClient};
use bss_context::RequestCtx;
use bss_db::PolicyViolation;
use serde_json::{json, Value};

use crate::error::ApiError;
use crate::repo::BundleBalanceRow;

fn upstream(e: ClientError) -> ApiError {
    ApiError::Internal(format!("upstream error: {e}"))
}

// ── create ──────────────────────────────────────────────────────────────────

/// Customer must exist and be active or pending.
pub async fn check_customer_exists(customer_id: &str, crm: &CrmClient) -> Result<(), ApiError> {
    let customer = match crm.get_customer(customer_id).await {
        Ok(v) => v,
        Err(ClientError::NotFound(_)) => {
            return Err(PolicyViolation::with_context(
                "subscription.create.requires_customer",
                format!("Customer {customer_id} not found"),
                json!({ "customer_id": customer_id }),
            )
            .into());
        }
        Err(e) => return Err(upstream(e)),
    };
    let status = customer.get("status").and_then(Value::as_str).unwrap_or("");
    if status != "active" && status != "pending" {
        return Err(PolicyViolation::with_context(
            "subscription.create.requires_customer",
            format!("Customer {customer_id} is {status}, must be active or pending"),
            json!({ "customer_id": customer_id, "status": status }),
        )
        .into());
    }
    Ok(())
}

/// Both MSISDN and eSIM must be in a reserved (or assigned) state.
pub async fn check_msisdn_and_esim_reserved(
    msisdn: &str,
    iccid: &str,
    inventory: &InventoryClient,
) -> Result<(), ApiError> {
    let rule = "subscription.create.msisdn_and_esim_reserved";
    let msisdn_info = match inventory.get_msisdn(msisdn).await {
        Ok(v) => v,
        Err(ClientError::NotFound(_)) => {
            return Err(PolicyViolation::with_context(
                rule,
                format!("MSISDN {msisdn} not found"),
                json!({ "msisdn": msisdn }),
            )
            .into());
        }
        Err(e) => return Err(upstream(e)),
    };
    let m_status = msisdn_info.get("status").and_then(Value::as_str);
    if !matches!(m_status, Some("reserved") | Some("assigned")) {
        return Err(PolicyViolation::with_context(
            rule,
            format!(
                "MSISDN {msisdn} is {}, must be reserved",
                m_status.unwrap_or("null")
            ),
            json!({ "msisdn": msisdn, "status": m_status }),
        )
        .into());
    }

    let esim_info = match inventory.get_esim(iccid).await {
        Ok(v) => v,
        Err(ClientError::NotFound(_)) => {
            return Err(PolicyViolation::with_context(
                rule,
                format!("eSIM {iccid} not found"),
                json!({ "iccid": iccid }),
            )
            .into());
        }
        Err(e) => return Err(upstream(e)),
    };
    let e_state = esim_info
        .get("profileState")
        .or_else(|| esim_info.get("profile_state"))
        .and_then(Value::as_str);
    if !matches!(e_state, Some("reserved") | Some("assigned")) {
        return Err(PolicyViolation::with_context(
            rule,
            format!("eSIM {iccid} is not reserved"),
            json!({ "iccid": iccid }),
        )
        .into());
    }
    Ok(())
}

// ── renew ───────────────────────────────────────────────────────────────────

pub fn check_renew_allowed(state: &str) -> Result<(), ApiError> {
    if state != "active" && state != "blocked" {
        return Err(PolicyViolation::with_context(
            "subscription.renew.only_if_active_or_blocked",
            format!("Cannot renew subscription in state '{state}'"),
            json!({ "state": state }),
        )
        .into());
    }
    Ok(())
}

// ── plan change ─────────────────────────────────────────────────────────────

pub fn check_subscription_active_or_pending_renewal(state: &str) -> Result<(), ApiError> {
    if state != "active" {
        return Err(PolicyViolation::with_context(
            "subscription.plan_change.not_eligible_state",
            format!("Cannot schedule plan change in state '{state}'"),
            json!({ "state": state }),
        )
        .into());
    }
    Ok(())
}

/// Target offering must be sellable at `active_at` (the caller's clock moment).
pub async fn check_offering_sellable_now(
    catalog: &CatalogClient,
    active_at: &str,
    new_offering_id: &str,
) -> Result<(), ApiError> {
    let active = catalog
        .list_active_offerings(active_at)
        .await
        .map_err(upstream)?;
    let found = active
        .as_array()
        .map(|arr| {
            arr.iter()
                .any(|o| o.get("id").and_then(Value::as_str) == Some(new_offering_id))
        })
        .unwrap_or(false);
    if !found {
        return Err(PolicyViolation::with_context(
            "subscription.plan_change.target_not_sellable_now",
            format!(
                "Offering {new_offering_id} is not currently sellable and cannot be a plan-change target"
            ),
            json!({ "new_offering_id": new_offering_id }),
        )
        .into());
    }
    Ok(())
}

pub fn check_not_same_offering(current: &str, new: &str) -> Result<(), ApiError> {
    if current == new {
        return Err(PolicyViolation::with_context(
            "subscription.plan_change.same_offering",
            "Cannot schedule a plan change to the current plan",
            json!({ "current_offering_id": current, "new_offering_id": new }),
        )
        .into());
    }
    Ok(())
}

pub fn check_no_pending_change(pending_offering_id: Option<&str>) -> Result<(), ApiError> {
    if let Some(pid) = pending_offering_id {
        return Err(PolicyViolation::with_context(
            "subscription.plan_change.already_pending",
            "A plan change is already pending. Cancel it first if you want to schedule a different one.",
            json!({ "pending_offering_id": pid }),
        )
        .into());
    }
    Ok(())
}

/// v0.7 — admin-only gate, sourced from the request context. The default context
/// grants `roles=["admin"]`, so this is permissive until Phase 12 wires real RBAC.
pub fn check_admin_role(ctx: &RequestCtx) -> Result<(), ApiError> {
    if !ctx.roles.iter().any(|r| r == "admin") {
        return Err(PolicyViolation::with_context(
            "subscription.admin_only",
            "This operation requires the admin role",
            json!({ "actor": ctx.actor, "roles": ctx.roles }),
        )
        .into());
    }
    Ok(())
}

/// Resolve the active price row for the target offering, wrapping the catalog-side
/// policy error into the subscription rule namespace.
pub async fn fetch_active_price_for_target(
    catalog: &CatalogClient,
    new_offering_id: &str,
) -> Result<Value, ApiError> {
    match catalog.get_active_price(new_offering_id).await {
        Ok(v) => Ok(v),
        Err(ClientError::Policy(pv)) => Err(PolicyViolation::with_context(
            "subscription.plan_change.target_no_active_price",
            format!("No active price for {new_offering_id} at this moment"),
            json!({ "new_offering_id": new_offering_id, "underlying": pv.rule }),
        )
        .into()),
        Err(e) => Err(upstream(e)),
    }
}

// ── usage (roaming) ─────────────────────────────────────────────────────────

/// Reject roaming usage when there's no roaming balance or it's exhausted.
/// Subscription state is intentionally NOT changed by this rejection — home data
/// is unaffected (v0.17 doctrine).
///
/// Returns the raw `PolicyViolation` (not `ApiError`) so the consumer can read
/// `.rule` / `.context` into the `usage.rejected` event payload (mirroring the
/// oracle's `reason=exc.rule, **exc.context`).
pub fn check_roaming_balance_required(
    subscription_id: &str,
    balance: Option<&BundleBalanceRow>,
    consumed_quantity: i64,
) -> Result<(), PolicyViolation> {
    let rule = "subscription.usage_rated.roaming_balance_required";
    match balance {
        None => Err(PolicyViolation::with_context(
            rule,
            format!("Subscription {subscription_id} has no data_roaming allowance — roaming usage rejected"),
            json!({ "subscription_id": subscription_id, "consumed_quantity": consumed_quantity }),
        )),
        Some(b) if b.total != -1 && (b.total - b.consumed) <= 0 => {
            Err(PolicyViolation::with_context(
                rule,
                format!("Subscription {subscription_id} data_roaming balance exhausted — roaming usage rejected"),
                json!({
                    "subscription_id": subscription_id,
                    "remaining": b.total - b.consumed,
                    "consumed_quantity": consumed_quantity,
                }),
            ))
        }
        Some(_) => Ok(()),
    }
}

// ── VAS ─────────────────────────────────────────────────────────────────────

pub fn check_not_terminated(state: &str) -> Result<(), ApiError> {
    if state == "terminated" {
        return Err(PolicyViolation::with_context(
            "subscription.vas_purchase.not_if_terminated",
            "Cannot purchase VAS on terminated subscription",
            json!({ "state": state }),
        )
        .into());
    }
    Ok(())
}

/// VAS offering must exist. Returns the VAS spec document.
pub async fn check_vas_offering_sellable(
    vas_offering_id: &str,
    catalog: &CatalogClient,
) -> Result<Value, ApiError> {
    match catalog.get_vas(vas_offering_id).await {
        Ok(v) => Ok(v),
        Err(ClientError::NotFound(_)) => Err(PolicyViolation::with_context(
            "subscription.vas_purchase.vas_offering_sellable",
            format!("VAS offering {vas_offering_id} not found"),
            json!({ "vas_offering_id": vas_offering_id }),
        )
        .into()),
        Err(e) => Err(upstream(e)),
    }
}
