//! Subscription orchestration — port of `app.services.subscription_service`.
//!
//! Router/worker/consumer → this layer → policies → repo → event staging. HTTP
//! write paths do their external calls (customer/inventory/catalog/payment) first,
//! then run the DB writes in one transaction and re-read the aggregate for the
//! response. The consumer's `handle_usage_rated` runs on the safe consumer's
//! `&mut PgConnection` (bind_consumer owns the commit) with the balance row locked
//! `FOR UPDATE` — the block-on-exhaust decrement.

use std::str::FromStr;

use bss_clients::ClientError;
use bss_context::RequestCtx;
use bss_db::PolicyViolation;
use chrono::{DateTime, Duration, Utc};
use rust_decimal::Decimal;
use serde_json::{json, Value};
use sqlx::postgres::PgConnection;
use sqlx::PgPool;

use crate::domain::{
    add_allowance, consume, initial_discount_remaining, is_exhausted, is_valid_transition,
    BalanceSnapshot, PRIMARY_ALLOWANCE_TYPE,
};
use crate::error::ApiError;
use crate::events::stage;
use crate::money::apply_discount;
use crate::policies::{
    check_admin_role, check_customer_exists, check_msisdn_and_esim_reserved,
    check_no_pending_change, check_not_same_offering, check_not_terminated,
    check_offering_sellable_now, check_renew_allowed, check_roaming_balance_required,
    check_subscription_active_or_pending_renewal, check_vas_offering_sellable,
    fetch_active_price_for_target,
};
use crate::repo::{self, SubscriptionFull, SubscriptionRow};
use crate::schemas::SubscriptionCreateRequest;
use crate::state::AppState;

const PERIOD_DAYS: i64 = 30;

fn upstream(e: ClientError) -> ApiError {
    ApiError::Internal(format!("upstream error: {e}"))
}

fn not_found(sub_id: &str) -> ApiError {
    PolicyViolation::with_context(
        "subscription.not_found",
        format!("Subscription {sub_id} not found"),
        json!({ "subscription_id": sub_id }),
    )
    .into()
}

/// `str(value)` for a money field that may be a JSON float or string — matches
/// Python's `Decimal(str(...))` seed string (`25.0`, not `25`).
fn money_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// (allowance_type, quantity, unit) from an offering's `bundleAllowance`.
fn allowance_specs(offering: &Value) -> Vec<(String, i64, String)> {
    offering
        .get("bundleAllowance")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .map(|a| {
                    (
                        a.get("allowanceType")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string(),
                        a.get("quantity").and_then(Value::as_i64).unwrap_or(0),
                        a.get("unit")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string(),
                    )
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Insert a state-history row (`changed_by = "system"`, as the oracle's
/// `_transition` hardcodes). `event_time` defaults server-side.
async fn add_state_history(
    conn: &mut PgConnection,
    sub_id: &str,
    from_state: &str,
    to_state: &str,
    reason: &str,
) -> Result<(), ApiError> {
    sqlx::query(
        "INSERT INTO subscription.subscription_state_history \
         (subscription_id, from_state, to_state, changed_by, reason) VALUES ($1,$2,$3,'system',$4)",
    )
    .bind(sub_id)
    .bind(from_state)
    .bind(to_state)
    .bind(reason)
    .execute(conn)
    .await?;
    Ok(())
}

/// Validate an FSM trigger and return the destination state. Mirrors the oracle's
/// `_transition` guard (raises `subscription.transition.invalid`).
fn next_state(from_state: &str, trigger: &str) -> Result<&'static str, ApiError> {
    if !is_valid_transition(from_state, trigger) {
        return Err(PolicyViolation::with_context(
            "subscription.transition.invalid",
            format!("Cannot trigger '{trigger}' from state '{from_state}'"),
            json!({ "state": from_state, "trigger": trigger }),
        )
        .into());
    }
    crate::domain::get_next_state(from_state, trigger)
        .ok_or_else(|| ApiError::Internal("transition produced no state".into()))
}

// ── reads ───────────────────────────────────────────────────────────────────

pub async fn get(pool: &PgPool, sub_id: &str) -> Result<Option<SubscriptionFull>, ApiError> {
    repo::get(pool, sub_id).await
}

pub async fn get_by_msisdn(
    pool: &PgPool,
    msisdn: &str,
) -> Result<Option<SubscriptionFull>, ApiError> {
    repo::get_by_msisdn(pool, msisdn).await
}

pub async fn list_for_customer(
    pool: &PgPool,
    customer_id: &str,
) -> Result<Vec<SubscriptionFull>, ApiError> {
    repo::list_for_customer(pool, customer_id).await
}

// ── create ──────────────────────────────────────────────────────────────────

pub async fn create(
    st: &AppState,
    ctx: &RequestCtx,
    req: SubscriptionCreateRequest,
) -> Result<SubscriptionFull, ApiError> {
    let pool = &st.pool;

    // v1.2 idempotency — a repeat create for the same commercial order returns the
    // existing subscription WITHOUT charging the card again.
    if let Some(coid) = &req.commercial_order_id {
        if let Some(existing) = repo::get_by_commercial_order(pool, coid).await? {
            tracing::info!(
                commercial_order_id = coid,
                subscription_id = existing.sub.id,
                "subscription.create.idempotent_hit"
            );
            return Ok(existing);
        }
    }

    check_customer_exists(&req.customer_id, &st.crm).await?;
    check_msisdn_and_esim_reserved(&req.msisdn, &req.iccid, &st.inventory).await?;

    let offering = st
        .catalog
        .get_offering(&req.offering_id)
        .await
        .map_err(upstream)?;

    // Resolve price: snapshot (COM/SOM flow) or the offering's recurring price.
    let (amount, currency, offering_price_id) = match &req.price_snapshot {
        Some(ps) => (
            ps.price_amount,
            ps.price_currency.clone(),
            ps.price_offering_price_id.clone(),
        ),
        None => recurring_price(&offering, &req.offering_id)?,
    };

    let ps = req.price_snapshot.as_ref();
    let discount_type = ps.and_then(|p| p.discount_type.clone());
    let discount_value = ps.and_then(|p| p.discount_value);
    let discount_periods_total = ps.and_then(|p| p.discount_periods_total);
    let promo_code = ps.and_then(|p| p.promo_code.clone());
    let promo_offer_definition_id = ps.and_then(|p| p.promo_offer_definition_id.clone());

    // The activation charge is the effective price for period 1; `amount` (the full
    // base) is what's persisted.
    let charge_amount = match (&discount_type, discount_value) {
        (Some(dt), Some(dv)) => apply_discount(dt, dv, amount)
            .map_err(|e| ApiError::Internal(format!("discount math: {e}")))?,
        _ => amount,
    };

    // Activation always charges in SGD (the oracle hardcodes currency="SGD" here).
    let payment_result = st
        .payment
        .charge(
            &req.customer_id,
            &req.payment_method_id,
            &charge_amount.to_string(),
            "SGD",
            "activation",
        )
        .await
        .map_err(upstream)?;

    if payment_result.get("status").and_then(Value::as_str) != Some("approved") {
        // Release inventory on payment failure (best-effort).
        if let Err(e) = st.inventory.release_msisdn(&req.msisdn).await {
            tracing::warn!(msisdn = req.msisdn, error = %e, "inventory.release_msisdn.failed");
        }
        if let Err(e) = st.inventory.recycle_esim(&req.iccid).await {
            tracing::warn!(error = %e, "inventory.recycle_esim.failed");
        }
        return Err(PolicyViolation::with_context(
            "subscription.create.requires_payment_success",
            "Activation payment was declined",
            json!({
                "payment_status": payment_result.get("status"),
                "decline_reason": payment_result.get("declineReason"),
            }),
        )
        .into());
    }

    let now = bss_clock::now();
    let period_end = now + Duration::days(PERIOD_DAYS);
    let sub_id = repo::next_id(pool).await?;
    let discount_remaining =
        initial_discount_remaining(discount_type.as_deref(), discount_periods_total);

    // Assign inventory (best-effort; external state, independent of our tx).
    if let Err(e) = st.inventory.assign_msisdn(&req.msisdn).await {
        tracing::warn!(msisdn = req.msisdn, error = %e, "inventory.assign_msisdn.failed");
    }
    if let Err(e) = st
        .inventory
        .assign_msisdn_to_esim(&req.iccid, &req.msisdn)
        .await
    {
        tracing::warn!(error = %e, "inventory.assign_esim.failed");
    }

    let mut tx = pool.begin().await?;

    // Insert directly in the activated end-state (state='active' + period fields):
    // the observable result is identical to the oracle's insert-pending-then-
    // transition, and the pending→active history row below records the transition.
    sqlx::query(
        "INSERT INTO subscription.subscription \
         (id, customer_id, commercial_order_id, offering_id, msisdn, iccid, state, state_reason, \
          activated_at, current_period_start, current_period_end, next_renewal_at, \
          price_amount, price_currency, price_offering_price_id, \
          discount_type, discount_value, discount_periods_remaining, promo_code, \
          promo_offer_definition_id) \
         VALUES ($1,$2,$3,$4,$5,$6,'active','activation_payment_approved',$7,$7,$8,$8, \
          CAST($9 AS numeric),$10,$11,$12,CAST($13 AS numeric),$14,$15,$16)",
    )
    .bind(&sub_id)
    .bind(&req.customer_id)
    .bind(&req.commercial_order_id)
    .bind(&req.offering_id)
    .bind(&req.msisdn)
    .bind(&req.iccid)
    .bind(now)
    .bind(period_end)
    .bind(amount.to_string())
    .bind(&currency)
    .bind(&offering_price_id)
    .bind(&discount_type)
    .bind(discount_value.map(|d| d.to_string()))
    .bind(discount_remaining as i16)
    .bind(&promo_code)
    .bind(&promo_offer_definition_id)
    .execute(&mut *tx)
    .await?;

    for (atype, qty, unit) in allowance_specs(&offering) {
        let bal_id = format!("{sub_id}-{}", atype.to_uppercase());
        sqlx::query(
            "INSERT INTO subscription.bundle_balance \
             (id, subscription_id, allowance_type, total, consumed, unit, period_start, period_end) \
             VALUES ($1,$2,$3,$4,0,$5,$6,$7)",
        )
        .bind(&bal_id)
        .bind(&sub_id)
        .bind(&atype)
        .bind(qty)
        .bind(&unit)
        .bind(now)
        .bind(period_end)
        .execute(&mut *tx)
        .await?;
    }

    add_state_history(
        &mut tx,
        &sub_id,
        "pending",
        "active",
        "activation_payment_approved",
    )
    .await?;

    stage(
        &mut tx,
        ctx,
        "subscription.activated",
        "subscription",
        &sub_id,
        json!({
            "subscriptionId": sub_id,
            "customerId": req.customer_id,
            "offeringId": req.offering_id,
            "msisdn": req.msisdn,
            "iccid": req.iccid,
            "paymentAttemptId": payment_result.get("id").and_then(Value::as_str).unwrap_or(""),
            "periodStart": bss_clock::isoformat(now),
            "periodEnd": bss_clock::isoformat(period_end),
            "amountCharged": charge_amount.to_string(),
            "promoCode": promo_code,
        }),
    )
    .await?;

    tx.commit().await?;
    tracing::info!(subscription_id = sub_id, amount_charged = %charge_amount, "subscription.created");
    repo::get(pool, &sub_id)
        .await?
        .ok_or_else(|| ApiError::Internal("subscription vanished after create".into()))
}

/// Legacy price path — read the offering's recurring price row. Mirrors the
/// oracle's `productOfferingPrice` fallback + `requires_active_price` violation.
fn recurring_price(
    offering: &Value,
    offering_id: &str,
) -> Result<(Decimal, String, String), ApiError> {
    let prices = offering
        .get("productOfferingPrice")
        .and_then(Value::as_array);
    let recurring = prices.and_then(|arr| {
        arr.iter()
            .find(|p| p.get("priceType").and_then(Value::as_str) == Some("recurring"))
    });
    let Some(recurring) = recurring else {
        // No recurring row → amount 0 / SGD / no id → requires_active_price.
        return Err(PolicyViolation::with_context(
            "subscription.create.requires_active_price",
            format!("Offering {offering_id} has no recurring price row to snapshot"),
            json!({ "offering_id": offering_id }),
        )
        .into());
    };
    let tia = &recurring["price"]["taxIncludedAmount"];
    let amount = Decimal::from_str(&money_str(&tia["value"]))
        .map_err(|e| ApiError::Internal(format!("bad price value: {e}")))?;
    let currency = tia
        .get("unit")
        .and_then(Value::as_str)
        .unwrap_or("SGD")
        .to_string();
    let offering_price_id = recurring.get("id").and_then(Value::as_str);
    let Some(offering_price_id) = offering_price_id else {
        return Err(PolicyViolation::with_context(
            "subscription.create.requires_active_price",
            format!("Offering {offering_id} has no recurring price row to snapshot"),
            json!({ "offering_id": offering_id }),
        )
        .into());
    };
    Ok((amount, currency, offering_price_id.to_string()))
}

// ── usage.rated (consumer path, on the tx connection) ───────────────────────

pub async fn handle_usage_rated(
    conn: &mut PgConnection,
    ctx: &RequestCtx,
    subscription_id: &str,
    allowance_type: &str,
    consumed_quantity: i64,
    usage_event_id: &str,
) -> Result<(), ApiError> {
    let Some(sub) = repo::get_sub_on_conn(conn, subscription_id).await? else {
        tracing::warn!(
            subscription_id,
            usage_event_id,
            "usage.rated.subscription_not_found"
        );
        return Ok(());
    };
    // Belt-and-braces: drop events for a non-active subscription (replay/race).
    if sub.state != "active" {
        tracing::warn!(
            subscription_id,
            state = sub.state,
            usage_event_id,
            "usage.rated.subscription_not_active"
        );
        return Ok(());
    }

    let target = repo::get_balance_for_update(conn, subscription_id, allowance_type).await?;

    // v0.17 — roaming usage is policy-gated independently from home data.
    if allowance_type == "data_roaming" {
        if let Err(pv) =
            check_roaming_balance_required(subscription_id, target.as_ref(), consumed_quantity)
        {
            let mut payload = json!({
                "subscriptionId": subscription_id,
                "allowanceType": allowance_type,
                "usageEventId": usage_event_id,
                "reason": pv.rule,
            });
            if let (Some(obj), Some(ctx_obj)) = (payload.as_object_mut(), pv.context.as_object()) {
                for (k, v) in ctx_obj {
                    obj.insert(k.clone(), v.clone());
                }
            }
            stage(
                conn,
                ctx,
                "usage.rejected",
                "subscription",
                subscription_id,
                payload,
            )
            .await?;
            tracing::warn!(
                subscription_id,
                usage_event_id,
                "usage.rejected.roaming_balance_required"
            );
            return Ok(());
        }
    }

    let Some(target) = target else {
        tracing::warn!(
            subscription_id,
            allowance_type,
            "usage.rated.allowance_not_on_subscription"
        );
        return Ok(());
    };

    let snap = BalanceSnapshot {
        allowance_type: target.allowance_type.clone(),
        total: target.total,
        consumed: target.consumed,
        unit: target.unit.clone(),
    };
    let result = consume(&snap, consumed_quantity);
    sqlx::query("UPDATE subscription.bundle_balance SET consumed=$1, updated_at=now() WHERE id=$2")
        .bind(result.consumed)
        .bind(&target.id)
        .execute(&mut *conn)
        .await?;

    let all = repo::get_balances_on_conn(conn, subscription_id).await?;
    let snapshots: Vec<BalanceSnapshot> = all
        .iter()
        .map(|b| BalanceSnapshot {
            allowance_type: b.allowance_type.clone(),
            total: b.total,
            consumed: b.consumed,
            unit: b.unit.clone(),
        })
        .collect();

    let mut final_state = sub.state.clone();
    if is_exhausted(&snapshots, PRIMARY_ALLOWANCE_TYPE) {
        stage(
            conn,
            ctx,
            "subscription.exhausted",
            "subscription",
            subscription_id,
            json!({
                "subscriptionId": subscription_id,
                "allowanceType": allowance_type,
                "consumed": result.consumed,
                "total": result.total,
                "triggeringUsageEventId": usage_event_id,
            }),
        )
        .await?;
        let to = next_state(&sub.state, "exhaust")?;
        sqlx::query("UPDATE subscription.subscription SET state=$1, state_reason=$2, updated_at=now() WHERE id=$3")
            .bind(to)
            .bind("primary_allowance_exhausted")
            .bind(subscription_id)
            .execute(&mut *conn)
            .await?;
        add_state_history(
            conn,
            subscription_id,
            &sub.state,
            to,
            "primary_allowance_exhausted",
        )
        .await?;
        stage(
            conn,
            ctx,
            "subscription.blocked",
            "subscription",
            subscription_id,
            json!({ "subscriptionId": subscription_id, "reason": "exhausted" }),
        )
        .await?;
        final_state = to.to_string();
    }

    tracing::info!(
        subscription_id,
        allowance_type,
        consumed_quantity,
        usage_event_id,
        new_consumed = result.consumed,
        final_state,
        "usage.rated.applied"
    );
    Ok(())
}

// ── VAS purchase ────────────────────────────────────────────────────────────

pub async fn purchase_vas(
    st: &AppState,
    ctx: &RequestCtx,
    sub_id: &str,
    vas_offering_id: &str,
) -> Result<SubscriptionFull, ApiError> {
    let pool = &st.pool;
    let sub = repo::get(pool, sub_id)
        .await?
        .ok_or_else(|| not_found(sub_id))?;
    let sub = sub.sub;

    check_not_terminated(&sub.state)?;
    let vas = check_vas_offering_sellable(vas_offering_id, &st.catalog).await?;

    // Default payment method (isDefault, else first, else violation).
    let methods = st
        .payment
        .list_methods(&sub.customer_id)
        .await
        .map_err(upstream)?;
    let default_method = pick_default_method(&methods).ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "subscription.vas_purchase.requires_active_cof",
            "No active payment method found",
            json!({ "customer_id": sub.customer_id }),
        ))
    })?;

    let amount = Decimal::from_str(&money_str(vas.get("priceAmount").unwrap_or(&json!(0))))
        .map_err(|e| ApiError::Internal(format!("bad vas price: {e}")))?;
    let currency = vas.get("currency").and_then(Value::as_str).unwrap_or("SGD");
    let payment_result = st
        .payment
        .charge(
            &sub.customer_id,
            &default_method,
            &amount.to_string(),
            currency,
            "vas",
        )
        .await
        .map_err(upstream)?;
    if payment_result.get("status").and_then(Value::as_str) != Some("approved") {
        return Err(PolicyViolation::with_context(
            "subscription.vas_purchase.requires_active_cof",
            "VAS payment was declined",
            json!({
                "payment_status": payment_result.get("status"),
                "decline_reason": payment_result.get("declineReason"),
            }),
        )
        .into());
    }

    let now = bss_clock::now();
    let vas_id = format!(
        "{sub_id}-VAS-{}",
        uuid::Uuid::new_v4().simple().to_string()[..8].to_uppercase()
    );
    let expiry_hours = vas.get("expiryHours").and_then(Value::as_i64);
    let expires_at = expiry_hours.map(|h| now + Duration::hours(h));
    let allowance_qty = vas
        .get("allowanceQuantity")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let allowance_type = vas
        .get("allowanceType")
        .and_then(Value::as_str)
        .unwrap_or("data")
        .to_string();
    let allowance_unit = vas
        .get("allowanceUnit")
        .and_then(Value::as_str)
        .unwrap_or("mb")
        .to_string();
    let payment_attempt_id = payment_result.get("id").and_then(Value::as_str);

    let mut tx = pool.begin().await?;

    sqlx::query(
        "INSERT INTO subscription.vas_purchase \
         (id, subscription_id, vas_offering_id, payment_attempt_id, applied_at, expires_at, \
          allowance_added, allowance_type) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
    )
    .bind(&vas_id)
    .bind(sub_id)
    .bind(vas_offering_id)
    .bind(payment_attempt_id)
    .bind(now)
    .bind(expires_at)
    .bind(allowance_qty)
    .bind(&allowance_type)
    .execute(&mut *tx)
    .await?;

    // Apply allowance: materialize a new row when none exists, else top up.
    let balances = repo::get_balances_on_conn(&mut tx, sub_id).await?;
    let target = balances.iter().find(|b| b.allowance_type == allowance_type);
    match target {
        None if allowance_qty > 0 => {
            let bal_id = format!("{sub_id}-{}", allowance_type.to_uppercase());
            sqlx::query(
                "INSERT INTO subscription.bundle_balance \
                 (id, subscription_id, allowance_type, total, consumed, unit, period_start, period_end) \
                 VALUES ($1,$2,$3,$4,0,$5,$6,$7)",
            )
            .bind(&bal_id)
            .bind(sub_id)
            .bind(&allowance_type)
            .bind(allowance_qty)
            .bind(&allowance_unit)
            .bind(now)
            .bind(sub.current_period_end)
            .execute(&mut *tx)
            .await?;
        }
        Some(t) if allowance_qty > 0 && t.total != -1 => {
            let snap = BalanceSnapshot {
                allowance_type: t.allowance_type.clone(),
                total: t.total,
                consumed: t.consumed,
                unit: t.unit.clone(),
            };
            let updated = add_allowance(&snap, allowance_qty);
            sqlx::query(
                "UPDATE subscription.bundle_balance SET total=$1, updated_at=now() WHERE id=$2",
            )
            .bind(updated.total)
            .bind(&t.id)
            .execute(&mut *tx)
            .await?;
        }
        _ => {}
    }

    let previous_state = sub.state.clone();
    stage(
        &mut tx,
        ctx,
        "subscription.vas_purchased",
        "subscription",
        sub_id,
        json!({
            "subscriptionId": sub_id,
            "vasOfferingId": vas_offering_id,
            "paymentAttemptId": payment_attempt_id.unwrap_or(""),
            "allowanceType": allowance_type,
            "allowanceAdded": allowance_qty,
            "previousState": previous_state,
        }),
    )
    .await?;

    // Top-up transition: blocked → active (+unblocked event); active → active.
    if sub.state == "blocked" {
        let to = next_state(&sub.state, "top_up")?;
        sqlx::query("UPDATE subscription.subscription SET state=$1, state_reason='vas_top_up', updated_at=now() WHERE id=$2")
            .bind(to)
            .bind(sub_id)
            .execute(&mut *tx)
            .await?;
        add_state_history(&mut tx, sub_id, &sub.state, to, "vas_top_up").await?;
        stage(
            &mut tx,
            ctx,
            "subscription.unblocked",
            "subscription",
            sub_id,
            json!({ "subscriptionId": sub_id, "reason": "vas_top_up" }),
        )
        .await?;
    } else if sub.state == "active" {
        // top_up on active → still active (self-transition; history only).
        let to = next_state(&sub.state, "top_up")?;
        sqlx::query("UPDATE subscription.subscription SET state_reason='vas_top_up', updated_at=now() WHERE id=$1")
            .bind(sub_id)
            .execute(&mut *tx)
            .await?;
        add_state_history(&mut tx, sub_id, &sub.state, to, "vas_top_up").await?;
    }

    tx.commit().await?;
    repo::get(pool, sub_id)
        .await?
        .ok_or_else(|| ApiError::Internal("subscription vanished after vas".into()))
}

fn pick_default_method(methods: &Value) -> Option<String> {
    let arr = methods.as_array()?;
    let by_default = arr
        .iter()
        .find(|m| m.get("isDefault").and_then(Value::as_bool) == Some(true));
    let chosen = by_default.or_else(|| arr.first())?;
    chosen.get("id").and_then(Value::as_str).map(str::to_string)
}

// ── renew ───────────────────────────────────────────────────────────────────

pub async fn renew(
    st: &AppState,
    ctx: &RequestCtx,
    sub_id: &str,
) -> Result<SubscriptionFull, ApiError> {
    let pool = &st.pool;
    let sub = repo::get(pool, sub_id)
        .await?
        .ok_or_else(|| not_found(sub_id))?
        .sub;

    check_renew_allowed(&sub.state)?;

    let now = bss_clock::now();
    let applying_pending = sub.pending_offering_id.is_some()
        && sub.pending_effective_at.map(|e| e <= now).unwrap_or(false);

    // Resolve the amount/currency/price-id + target offering for this renewal.
    let (amount, currency, offering_price_id, target_offering_id) = if applying_pending {
        let price_id = sub.pending_offering_price_id.clone().unwrap_or_default();
        let new_price = st
            .catalog
            .get_offering_price(&price_id)
            .await
            .map_err(upstream)?;
        let tia = &new_price["price"]["taxIncludedAmount"];
        let amount = Decimal::from_str(&money_str(&tia["value"]))
            .map_err(|e| ApiError::Internal(format!("bad price value: {e}")))?;
        let currency = tia
            .get("unit")
            .and_then(Value::as_str)
            .unwrap_or("SGD")
            .to_string();
        (
            amount,
            currency,
            price_id,
            sub.pending_offering_id.clone().unwrap_or_default(),
        )
    } else {
        (
            sub.price_amount,
            sub.price_currency.clone(),
            sub.price_offering_price_id.clone(),
            sub.offering_id.clone(),
        )
    };

    let offering = st
        .catalog
        .get_offering(&target_offering_id)
        .await
        .map_err(upstream)?;

    // v1.1 — apply the promo discount while the counter is live (and not pivoting).
    let applying_discount = !applying_pending
        && sub.discount_type.is_some()
        && sub.discount_value.is_some()
        && sub.discount_periods_remaining != 0;
    let charge_amount = if applying_discount {
        apply_discount(
            sub.discount_type.as_deref().unwrap_or(""),
            sub.discount_value.unwrap_or(Decimal::ZERO),
            amount,
        )
        .map_err(|e| ApiError::Internal(format!("discount math: {e}")))?
    } else {
        amount
    };

    let methods = st
        .payment
        .list_methods(&sub.customer_id)
        .await
        .map_err(upstream)?;
    let default_method = pick_default_method(&methods).ok_or_else(|| {
        ApiError::from(PolicyViolation::with_context(
            "subscription.renew.no_payment_method",
            "No active payment method found for renewal",
            json!({ "customer_id": sub.customer_id }),
        ))
    })?;

    let payment_result = st
        .payment
        .charge(
            &sub.customer_id,
            &default_method,
            &charge_amount.to_string(),
            &currency,
            "renewal",
        )
        .await
        .map_err(upstream)?;

    if payment_result.get("status").and_then(Value::as_str) != Some("approved") {
        // Renewal failed → block. Pending fields are intentionally NOT cleared.
        let mut tx = pool.begin().await?;
        let to = next_state(&sub.state, "renew_fail")?;
        sqlx::query("UPDATE subscription.subscription SET state=$1, state_reason='renewal_payment_declined', updated_at=now() WHERE id=$2")
            .bind(to)
            .bind(sub_id)
            .execute(&mut *tx)
            .await?;
        add_state_history(&mut tx, sub_id, &sub.state, to, "renewal_payment_declined").await?;
        let attempt_id = payment_result
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("");
        stage(&mut tx, ctx, "subscription.renew_failed", "subscription", sub_id, json!({
            "subscriptionId": sub_id, "reason": "payment_declined", "paymentAttemptId": attempt_id,
        })).await?;
        if applying_pending {
            stage(
                &mut tx,
                ctx,
                "subscription.plan_change_payment_failed",
                "subscription",
                sub_id,
                json!({
                    "subscriptionId": sub_id,
                    "currentOfferingId": sub.offering_id,
                    "pendingOfferingId": sub.pending_offering_id,
                    "paymentAttemptId": attempt_id,
                }),
            )
            .await?;
        }
        stage(
            &mut tx,
            ctx,
            "subscription.blocked",
            "subscription",
            sub_id,
            json!({
                "subscriptionId": sub_id, "reason": "renew_failed",
            }),
        )
        .await?;
        tx.commit().await?;
        return repo::get(pool, sub_id)
            .await?
            .ok_or_else(|| ApiError::Internal("subscription vanished after renew_fail".into()));
    }

    // Renewal succeeded — reset balances.
    let period_end = now + Duration::days(PERIOD_DAYS);
    let new_specs = allowance_specs(&offering); // (type, qty, unit)

    let mut tx = pool.begin().await?;
    let balances = repo::get_balances_on_conn(&mut tx, sub_id).await?;
    let existing_types: std::collections::HashSet<String> =
        balances.iter().map(|b| b.allowance_type.clone()).collect();

    for bal in &balances {
        if let Some((_, qty, _)) = new_specs.iter().find(|(t, _, _)| *t == bal.allowance_type) {
            sqlx::query("UPDATE subscription.bundle_balance SET total=$1, consumed=0, period_start=$2, period_end=$3, updated_at=now() WHERE id=$4")
                .bind(*qty)
                .bind(now)
                .bind(period_end)
                .bind(&bal.id)
                .execute(&mut *tx)
                .await?;
        } else if applying_pending {
            // Old allowance type not in the new plan → zero it out.
            sqlx::query("UPDATE subscription.bundle_balance SET total=0, consumed=0, period_start=$1, period_end=$2, updated_at=now() WHERE id=$3")
                .bind(now)
                .bind(period_end)
                .bind(&bal.id)
                .execute(&mut *tx)
                .await?;
        }
    }

    if applying_pending {
        for (atype, qty, unit) in &new_specs {
            if existing_types.contains(atype) {
                continue;
            }
            let bal_id = format!("{sub_id}-{}", atype.to_uppercase());
            sqlx::query(
                "INSERT INTO subscription.bundle_balance \
                 (id, subscription_id, allowance_type, total, consumed, unit, period_start, period_end) \
                 VALUES ($1,$2,$3,$4,0,$5,$6,$7)",
            )
            .bind(&bal_id)
            .bind(sub_id)
            .bind(atype)
            .bind(*qty)
            .bind(unit)
            .bind(now)
            .bind(period_end)
            .execute(&mut *tx)
            .await?;
        }
    }

    // Apply pending pivot: swap offering + snapshot, clear pending + promo.
    let previous_offering_id = sub.offering_id.clone();
    let was_price_migration =
        applying_pending && sub.pending_offering_id.as_deref() == Some(&sub.offering_id);
    let new_offering_id = if applying_pending {
        sub.pending_offering_id
            .clone()
            .unwrap_or_else(|| sub.offering_id.clone())
    } else {
        sub.offering_id.clone()
    };

    let to = next_state(&sub.state, "renew")?;
    if applying_pending {
        sqlx::query(
            "UPDATE subscription.subscription SET \
             state=$1, state_reason='renewal_payment_approved', \
             offering_id=$2, price_amount=CAST($3 AS numeric), price_currency=$4, price_offering_price_id=$5, \
             pending_offering_id=NULL, pending_offering_price_id=NULL, pending_effective_at=NULL, \
             discount_type=NULL, discount_value=NULL, discount_periods_remaining=0, \
             promo_code=NULL, promo_offer_definition_id=NULL, \
             current_period_start=$6, current_period_end=$7, next_renewal_at=$7, updated_at=now() \
             WHERE id=$8",
        )
        .bind(to)
        .bind(&new_offering_id)
        .bind(amount.to_string())
        .bind(&currency)
        .bind(&offering_price_id)
        .bind(now)
        .bind(period_end)
        .bind(sub_id)
        .execute(&mut *tx)
        .await?;
    } else {
        // Vanilla renewal: decrement the discount counter (perpetual -1 never
        // decrements) and advance the period.
        let new_remaining = if applying_discount && sub.discount_periods_remaining > 0 {
            sub.discount_periods_remaining - 1
        } else {
            sub.discount_periods_remaining
        };
        sqlx::query(
            "UPDATE subscription.subscription SET \
             state=$1, state_reason='renewal_payment_approved', discount_periods_remaining=$2, \
             current_period_start=$3, current_period_end=$4, next_renewal_at=$4, updated_at=now() \
             WHERE id=$5",
        )
        .bind(to)
        .bind(new_remaining)
        .bind(now)
        .bind(period_end)
        .bind(sub_id)
        .execute(&mut *tx)
        .await?;
    }
    add_state_history(&mut tx, sub_id, &sub.state, to, "renewal_payment_approved").await?;

    let attempt_id = payment_result
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("");
    stage(
        &mut tx,
        ctx,
        "subscription.renewed",
        "subscription",
        sub_id,
        json!({
            "subscriptionId": sub_id,
            "offeringId": new_offering_id,
            "paymentAttemptId": attempt_id,
            "periodStart": bss_clock::isoformat(now),
            "periodEnd": bss_clock::isoformat(period_end),
            "priceSnapshot": {
                "priceAmount": amount.to_string(),
                "priceCurrency": currency,
                "priceOfferingPriceId": offering_price_id,
            },
            "amountCharged": charge_amount.to_string(),
            "discountApplied": applying_discount,
        }),
    )
    .await?;

    if applying_pending {
        let event_name = if was_price_migration {
            "subscription.price_migrated"
        } else {
            "subscription.plan_changed"
        };
        stage(
            &mut tx,
            ctx,
            event_name,
            "subscription",
            sub_id,
            json!({
                "subscriptionId": sub_id,
                "previousOfferingId": previous_offering_id,
                "newOfferingId": new_offering_id,
                "newPriceAmount": amount.to_string(),
                "newPriceCurrency": currency,
                "newPriceOfferingPriceId": offering_price_id,
            }),
        )
        .await?;
    }

    tx.commit().await?;
    repo::get(pool, sub_id)
        .await?
        .ok_or_else(|| ApiError::Internal("subscription vanished after renew".into()))
}

// ── plan change / cancel ────────────────────────────────────────────────────

pub async fn schedule_plan_change(
    st: &AppState,
    ctx: &RequestCtx,
    sub_id: &str,
    new_offering_id: &str,
) -> Result<SubscriptionFull, ApiError> {
    let pool = &st.pool;
    let sub = repo::get(pool, sub_id)
        .await?
        .ok_or_else(|| not_found(sub_id))?
        .sub;

    check_subscription_active_or_pending_renewal(&sub.state)?;
    check_not_same_offering(&sub.offering_id, new_offering_id)?;
    check_no_pending_change(sub.pending_offering_id.as_deref())?;
    let active_at = bss_clock::isoformat(bss_clock::now());
    check_offering_sellable_now(&st.catalog, &active_at, new_offering_id).await?;
    let new_price = fetch_active_price_for_target(&st.catalog, new_offering_id).await?;
    let new_price_id = new_price
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();

    let effective_at = sub.next_renewal_at;

    let mut tx = pool.begin().await?;
    sqlx::query("UPDATE subscription.subscription SET pending_offering_id=$1, pending_offering_price_id=$2, pending_effective_at=$3, updated_at=now() WHERE id=$4")
        .bind(new_offering_id)
        .bind(&new_price_id)
        .bind(effective_at)
        .bind(sub_id)
        .execute(&mut *tx)
        .await?;

    let tia = &new_price["price"]["taxIncludedAmount"];
    stage(
        &mut tx,
        ctx,
        "subscription.plan_change_scheduled",
        "subscription",
        sub_id,
        json!({
            "subscriptionId": sub_id,
            "currentOfferingId": sub.offering_id,
            "newOfferingId": new_offering_id,
            "newPriceAmount": money_str(&tia["value"]),
            "newPriceCurrency": tia.get("unit").and_then(Value::as_str).unwrap_or("SGD"),
            "effectiveAt": effective_at.map(bss_clock::isoformat),
        }),
    )
    .await?;

    tx.commit().await?;
    tracing::info!(
        subscription_id = sub_id,
        new_offering_id,
        "subscription.plan_change.scheduled"
    );
    repo::get(pool, sub_id)
        .await?
        .ok_or_else(|| ApiError::Internal("subscription vanished after schedule".into()))
}

pub async fn cancel_pending_plan_change(
    st: &AppState,
    ctx: &RequestCtx,
    sub_id: &str,
) -> Result<SubscriptionFull, ApiError> {
    let pool = &st.pool;
    let sub = repo::get(pool, sub_id)
        .await?
        .ok_or_else(|| not_found(sub_id))?
        .sub;

    if sub.pending_offering_id.is_none() {
        tracing::info!(
            subscription_id = sub_id,
            "subscription.plan_change.cancel.noop"
        );
        return repo::get(pool, sub_id)
            .await?
            .ok_or_else(|| ApiError::Internal("subscription vanished".into()));
    }

    let previous_pending = json!({
        "offeringId": sub.pending_offering_id,
        "offeringPriceId": sub.pending_offering_price_id,
        "effectiveAt": sub.pending_effective_at.map(bss_clock::isoformat),
    });

    let mut tx = pool.begin().await?;
    sqlx::query("UPDATE subscription.subscription SET pending_offering_id=NULL, pending_offering_price_id=NULL, pending_effective_at=NULL, updated_at=now() WHERE id=$1")
        .bind(sub_id)
        .execute(&mut *tx)
        .await?;
    stage(
        &mut tx,
        ctx,
        "subscription.plan_change_cancelled",
        "subscription",
        sub_id,
        json!({
            "subscriptionId": sub_id, "previousPending": previous_pending,
        }),
    )
    .await?;
    tx.commit().await?;
    tracing::info!(
        subscription_id = sub_id,
        "subscription.plan_change.cancelled"
    );
    repo::get(pool, sub_id)
        .await?
        .ok_or_else(|| ApiError::Internal("subscription vanished after cancel".into()))
}

// ── operator price migration ────────────────────────────────────────────────

pub struct MigrateResult {
    pub count: usize,
    pub subscription_ids: Vec<String>,
}

pub async fn migrate_subscriptions_to_price(
    st: &AppState,
    ctx: &RequestCtx,
    offering_id: &str,
    new_price_id: &str,
    effective_from: DateTime<Utc>,
    notice_days: i64,
    initiated_by: &str,
) -> Result<MigrateResult, ApiError> {
    check_admin_role(ctx)?;
    let pool = &st.pool;

    let new_price = match st.catalog.get_offering_price(new_price_id).await {
        Ok(v) => v,
        Err(ClientError::NotFound(_)) => {
            return Err(PolicyViolation::with_context(
                "subscription.migrate_price.unknown_price",
                format!("Price {new_price_id} not found in catalog"),
                json!({ "new_price_id": new_price_id }),
            )
            .into());
        }
        Err(e) => return Err(upstream(e)),
    };

    let offering = st
        .catalog
        .get_offering(offering_id)
        .await
        .map_err(upstream)?;
    let belongs = offering
        .get("productOfferingPrice")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .any(|p| p.get("id").and_then(Value::as_str) == Some(new_price_id))
        })
        .unwrap_or(false);
    if !belongs {
        return Err(PolicyViolation::with_context(
            "subscription.migrate_price.price_not_on_offering",
            format!("Price {new_price_id} does not belong to offering {offering_id}"),
            json!({ "new_price_id": new_price_id, "offering_id": offering_id }),
        )
        .into());
    }

    let tia = &new_price["price"]["taxIncludedAmount"];
    let new_amount = money_str(&tia["value"]);
    let new_currency = tia
        .get("unit")
        .and_then(Value::as_str)
        .unwrap_or("SGD")
        .to_string();
    let effective_at = effective_from + Duration::days(notice_days);

    let affected = repo::list_active_for_offering(pool, offering_id).await?;
    let mut affected_ids = Vec::with_capacity(affected.len());

    let mut tx = pool.begin().await?;
    for sub in &affected {
        let old_amount = sub.price_amount.to_string();
        sqlx::query("UPDATE subscription.subscription SET pending_offering_id=$1, pending_offering_price_id=$2, pending_effective_at=$3, updated_at=now() WHERE id=$4")
            .bind(&sub.offering_id) // same plan
            .bind(new_price_id)
            .bind(effective_at)
            .bind(&sub.id)
            .execute(&mut *tx)
            .await?;

        stage(
            &mut tx,
            ctx,
            "subscription.price_migration_scheduled",
            "subscription",
            &sub.id,
            json!({
                "subscriptionId": sub.id,
                "offeringId": sub.offering_id,
                "oldAmount": old_amount,
                "newAmount": new_amount,
                "newCurrency": new_currency,
                "effectiveAt": bss_clock::isoformat(effective_at),
                "initiatedBy": initiated_by,
            }),
        )
        .await?;
        stage(
            &mut tx,
            ctx,
            "notification.requested",
            "subscription",
            &sub.id,
            json!({
                "customerId": sub.customer_id,
                "channel": "email",
                "template": "price_migration_notice",
                "templateArgs": {
                    "subscriptionId": sub.id,
                    "offeringId": sub.offering_id,
                    "oldAmount": old_amount,
                    "newAmount": new_amount,
                    "currency": new_currency,
                    "effectiveAt": bss_clock::isoformat(effective_at),
                },
            }),
        )
        .await?;
        affected_ids.push(sub.id.clone());
    }
    tx.commit().await?;
    tracing::info!(
        offering_id,
        new_price_id,
        count = affected_ids.len(),
        initiated_by,
        "subscription.price_migration.scheduled"
    );
    Ok(MigrateResult {
        count: affected_ids.len(),
        subscription_ids: affected_ids,
    })
}

// ── terminate ───────────────────────────────────────────────────────────────

pub async fn terminate(
    st: &AppState,
    ctx: &RequestCtx,
    sub_id: &str,
    reason: &str,
    release_inventory: bool,
) -> Result<SubscriptionFull, ApiError> {
    let pool = &st.pool;
    let sub: SubscriptionRow = repo::get(pool, sub_id)
        .await?
        .ok_or_else(|| not_found(sub_id))?
        .sub;

    if !is_valid_transition(&sub.state, "terminate") {
        return Err(PolicyViolation::with_context(
            "subscription.terminate.invalid_state",
            format!("Cannot terminate subscription in state '{}'", sub.state),
            json!({ "state": sub.state }),
        )
        .into());
    }

    let now = bss_clock::now();
    if release_inventory {
        if let Err(e) = st.inventory.release_msisdn(&sub.msisdn).await {
            tracing::warn!(msisdn = sub.msisdn, error = %e, "inventory.release_msisdn.failed");
        }
    }
    if let Err(e) = st.inventory.recycle_esim(&sub.iccid).await {
        tracing::warn!(error = %e, "inventory.recycle_esim.failed");
    }

    let mut tx = pool.begin().await?;
    let to = next_state(&sub.state, "terminate")?;
    sqlx::query("UPDATE subscription.subscription SET state=$1, state_reason=$2, terminated_at=$3, updated_at=now() WHERE id=$4")
        .bind(to)
        .bind(reason)
        .bind(now)
        .bind(sub_id)
        .execute(&mut *tx)
        .await?;
    add_state_history(&mut tx, sub_id, &sub.state, to, reason).await?;
    stage(
        &mut tx,
        ctx,
        "subscription.terminated",
        "subscription",
        sub_id,
        json!({
            "subscriptionId": sub_id,
            "customerId": sub.customer_id,
            "msisdn": sub.msisdn,
            "iccid": sub.iccid,
            "terminatedAt": bss_clock::isoformat(now),
            "reason": reason,
        }),
    )
    .await?;
    tx.commit().await?;
    tracing::info!(subscription_id = sub_id, reason, "subscription.terminated");
    repo::get(pool, sub_id)
        .await?
        .ok_or_else(|| ApiError::Internal("subscription vanished after terminate".into()))
}
