//! Order orchestration — port of `app.services.order_service.OrderService`.
//!
//! HTTP write paths (create/submit/cancel) run in a transaction and re-read the
//! aggregate for the response. The two consumer handlers
//! (`handle_service_order_completed`/`_failed`) run on the safe consumer's
//! connection (bind_consumer owns the commit) and drive the promo consume
//! lifecycle (claim → redeem, or revoke on activation failure).

use std::str::FromStr;

use bss_clients::{
    CatalogClient, ClientError, CrmClient, LoyaltyClient, PaymentClient, SomClient,
    SubscriptionClient,
};
use bss_context::RequestCtx;
use bss_db::{PgPool, PolicyViolation};
use rust_decimal::Decimal;
use serde_json::{json, Value};
use sqlx::postgres::PgConnection;

use crate::domain::check_order_transition;
use crate::error::ApiError;
use crate::events::stage;
use crate::policies::{
    check_cancel_allowed_after_som, check_customer_exists, check_customer_has_payment_method,
    check_offering_currently_sellable,
};
use crate::repo::{self, ItemRow, OrderFull, OrderRow};

/// A resolved promo discount stamped as INTENT on the order item.
#[derive(Debug, Default, Clone)]
struct DiscountIntent {
    discount_code: Option<String>,
    offer_definition_id: Option<String>,
    discount_type: Option<String>,
    discount_value: Option<Decimal>,
    discount_periods_total: Option<i16>,
    promo_offer_id: Option<String>,
}

pub struct CreateOrder {
    pub customer_id: String,
    pub offering_id: String,
    pub msisdn_preference: Option<String>,
    pub notes: Option<String>,
    pub discount_code: Option<String>,
    pub skip_assigned_offer: bool,
}

// ── HTTP write paths ──────────────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
pub async fn create_order(
    pool: &PgPool,
    crm: &CrmClient,
    catalog: &CatalogClient,
    payment: &PaymentClient,
    ctx: &RequestCtx,
    req: CreateOrder,
) -> Result<OrderFull, ApiError> {
    check_customer_exists(&req.customer_id, crm).await?;
    let active_price = check_offering_currently_sellable(&req.offering_id, catalog).await?;
    check_customer_has_payment_method(&req.customer_id, payment).await?;

    // Snapshot — reproduce Python's `Decimal(str(value))`: the catalog value is a
    // JSON float (`25.0`), and `Value::to_string()` renders "25.0" (not "25"),
    // preserving the event-payload string exactly.
    let tia = &active_price["price"]["taxIncludedAmount"];
    let price_amount_str = money_str(&tia["value"]);
    let price_currency = tia
        .get("unit")
        .and_then(Value::as_str)
        .unwrap_or("SGD")
        .to_string();
    let price_offering_price_id = active_price
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let price_amount = Decimal::from_str(&price_amount_str)
        .map_err(|e| ApiError::Internal(format!("bad price value: {e}")))?;

    let discount = resolve_discount(
        catalog,
        &req.customer_id,
        &req.offering_id,
        req.discount_code.as_deref(),
        req.skip_assigned_offer,
    )
    .await;

    let order_id = repo::next_order_id(pool).await?;
    let item_id = repo::next_item_id(pool).await?;
    let now = bss_clock::now();

    let mut tx = pool.begin().await?;

    sqlx::query(
        "INSERT INTO order_mgmt.product_order (id, customer_id, state, order_date, msisdn_preference, notes) \
         VALUES ($1,$2,'acknowledged',$3,$4,$5)",
    )
    .bind(&order_id)
    .bind(&req.customer_id)
    .bind(now)
    .bind(&req.msisdn_preference)
    .bind(&req.notes)
    .execute(&mut *tx)
    .await?;

    sqlx::query(
        "INSERT INTO order_mgmt.order_item \
         (id, order_id, action, offering_id, state, price_amount, price_currency, \
          price_offering_price_id, discount_code, promo_offer_definition_id, discount_type, \
          discount_value, discount_periods_total, promo_offer_id) \
         VALUES ($1,$2,'add',$3,'acknowledged',CAST($4 AS numeric),$5,$6,$7,$8,$9,\
          CAST($10 AS numeric),$11,$12)",
    )
    .bind(&item_id)
    .bind(&order_id)
    .bind(&req.offering_id)
    .bind(&price_amount_str)
    .bind(&price_currency)
    .bind(&price_offering_price_id)
    .bind(&discount.discount_code)
    .bind(&discount.offer_definition_id)
    .bind(&discount.discount_type)
    .bind(discount.discount_value.map(|d| d.to_string()))
    .bind(discount.discount_periods_total)
    .bind(&discount.promo_offer_id)
    .execute(&mut *tx)
    .await?;

    add_state_history(
        &mut tx,
        &order_id,
        None,
        Some("acknowledged"),
        ctx,
        "order created",
    )
    .await?;

    stage(
        &mut tx,
        ctx,
        "order.acknowledged",
        "ProductOrder",
        &order_id,
        json!({
            "commercialOrderId": order_id,
            "customerId": req.customer_id,
            "offeringId": req.offering_id,
            "priceSnapshot": {
                "priceAmount": price_amount.to_string(),
                "priceCurrency": price_currency,
                "priceOfferingPriceId": price_offering_price_id,
            },
        }),
    )
    .await?;

    tx.commit().await?;
    repo::get(pool, &order_id)
        .await?
        .ok_or_else(|| ApiError::Internal("order vanished after create".into()))
}

/// Resolve a promo to stamp as INTENT. A valid typed code wins; else the best
/// eligible assigned offer (unless opted out). Any client error degrades to "no
/// discount" (never blocks the order).
async fn resolve_discount(
    catalog: &CatalogClient,
    customer_id: &str,
    offering_id: &str,
    discount_code: Option<&str>,
    skip_assigned_offer: bool,
) -> DiscountIntent {
    let mut res: Option<Value> = None;
    let mut code_to_claim: Option<String> = None;

    let resolved: Result<(), ClientError> = async {
        if let Some(code) = discount_code.filter(|c| !c.is_empty()) {
            let typed = catalog.validate_promo(code, offering_id, Some(customer_id)).await?;
            if typed.get("valid").and_then(Value::as_bool) == Some(true) {
                code_to_claim = Some(code.to_string());
                res = Some(typed);
            } else {
                tracing::info!(code, reason = ?typed.get("reason"), "order.promo.code_invalid_fallback_to_eligible");
            }
        }
        if res.is_none() && !skip_assigned_offer {
            let eligible = catalog.resolve_eligible_promo(customer_id, offering_id).await?;
            if eligible.get("valid").and_then(Value::as_bool) == Some(true) {
                code_to_claim = eligible.get("code").and_then(Value::as_str).map(str::to_string);
                res = Some(eligible);
            }
        }
        Ok(())
    }
    .await;

    if resolved.is_err() {
        tracing::warn!(code = ?discount_code, "order.promo.resolve_failed");
        return DiscountIntent::default();
    }
    let Some(res) = res else {
        return DiscountIntent::default();
    };
    let discount_value = res
        .get("discountValue")
        .and_then(Value::as_str)
        .and_then(|s| Decimal::from_str(s).ok());
    DiscountIntent {
        discount_code: code_to_claim,
        offer_definition_id: res
            .get("offerDefinitionId")
            .and_then(Value::as_str)
            .map(str::to_string),
        discount_type: res
            .get("discountType")
            .and_then(Value::as_str)
            .map(str::to_string),
        discount_value,
        discount_periods_total: res
            .get("discountPeriodsTotal")
            .and_then(Value::as_i64)
            .map(|n| n as i16),
        promo_offer_id: res
            .get("loyaltyOfferId")
            .and_then(Value::as_str)
            .map(str::to_string),
    }
}

pub async fn submit_order(
    pool: &PgPool,
    payment: &PaymentClient,
    ctx: &RequestCtx,
    order_id: &str,
) -> Result<OrderFull, ApiError> {
    let full = repo::get(pool, order_id)
        .await?
        .ok_or_else(|| order_not_found(order_id))?;
    check_order_transition(&full.order.state, "in_progress")?;

    let methods = payment
        .list_methods(&full.order.customer_id)
        .await
        .map_err(upstream)?;
    let payment_method_id = methods
        .as_array()
        .and_then(|a| a.first())
        .and_then(|m| m.get("id"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();

    let item = full.items.first();
    let offering_id = item.map(|i| i.offering_id.clone()).unwrap_or_default();
    let price_snapshot = item.and_then(item_price_snapshot);

    let old_state = full.order.state.clone();
    let mut tx = pool.begin().await?;
    sqlx::query(
        "UPDATE order_mgmt.product_order SET state='in_progress', updated_at=now() WHERE id=$1",
    )
    .bind(order_id)
    .execute(&mut *tx)
    .await?;
    add_state_history(
        &mut tx,
        order_id,
        Some(&old_state),
        Some("in_progress"),
        ctx,
        "order submitted",
    )
    .await?;

    let mut payload = json!({
        "commercialOrderId": order_id,
        "customerId": full.order.customer_id,
        "offeringId": offering_id,
        "msisdnPreference": full.order.msisdn_preference,
        "paymentMethodId": payment_method_id,
    });
    if let Some(ps) = price_snapshot {
        payload["priceSnapshot"] = ps;
    }
    stage(
        &mut tx,
        ctx,
        "order.in_progress",
        "ProductOrder",
        order_id,
        payload,
    )
    .await?;

    tx.commit().await?;
    repo::get(pool, order_id)
        .await?
        .ok_or_else(|| ApiError::Internal("order vanished after submit".into()))
}

pub async fn cancel_order(
    pool: &PgPool,
    som: &SomClient,
    ctx: &RequestCtx,
    order_id: &str,
) -> Result<OrderFull, ApiError> {
    let full = repo::get(pool, order_id)
        .await?
        .ok_or_else(|| order_not_found(order_id))?;
    check_order_transition(&full.order.state, "cancelled")?;
    if full.order.state == "in_progress" {
        check_cancel_allowed_after_som(order_id, som).await?;
    }

    let old_state = full.order.state.clone();
    let now = bss_clock::now();
    let mut tx = pool.begin().await?;
    sqlx::query("UPDATE order_mgmt.product_order SET state='cancelled', completed_date=$2, updated_at=now() WHERE id=$1")
        .bind(order_id)
        .bind(now)
        .execute(&mut *tx)
        .await?;
    add_state_history(
        &mut tx,
        order_id,
        Some(&old_state),
        Some("cancelled"),
        ctx,
        "cancelled by user",
    )
    .await?;
    stage(
        &mut tx,
        ctx,
        "order.cancelled",
        "ProductOrder",
        order_id,
        json!({ "commercialOrderId": order_id, "customerId": full.order.customer_id }),
    )
    .await?;

    tx.commit().await?;
    repo::get(pool, order_id)
        .await?
        .ok_or_else(|| ApiError::Internal("order vanished after cancel".into()))
}

pub async fn get_order(pool: &PgPool, order_id: &str) -> Result<Option<OrderFull>, ApiError> {
    repo::get(pool, order_id).await
}

pub async fn list_orders(
    pool: &PgPool,
    customer_id: Option<&str>,
    state: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<OrderFull>, ApiError> {
    repo::list(pool, customer_id, state, limit, offset).await
}

// ── consumer handlers (on the safe consumer's connection) ─────────────────────

pub struct ServiceOrderCompleted {
    pub commercial_order_id: String,
    pub customer_id: String,
    pub offering_id: String,
    pub msisdn: String,
    pub iccid: String,
    pub payment_method_id: String,
    pub cfs_service_id: String,
    pub price_snapshot: Option<Value>,
}

pub async fn handle_service_order_completed(
    conn: &mut PgConnection,
    subscription: &SubscriptionClient,
    loyalty: Option<&LoyaltyClient>,
    ctx: &RequestCtx,
    p: ServiceOrderCompleted,
) -> Result<(), ApiError> {
    let Some((order, item)) = repo::get_for_update(conn, &p.commercial_order_id).await? else {
        tracing::warn!(
            commercial_order_id = p.commercial_order_id,
            reason = "order not found or not in_progress",
            "order.service_order_completed.skipped"
        );
        return Ok(());
    };
    if order.state != "in_progress" {
        tracing::warn!(
            commercial_order_id = p.commercial_order_id,
            reason = "order not found or not in_progress",
            "order.service_order_completed.skipped"
        );
        return Ok(());
    }

    // Resolve price snapshot: prefer the event payload, fall back to the item row.
    let mut price_snapshot = p.price_snapshot.clone().filter(|v| !v.is_null());
    if price_snapshot.is_none() {
        price_snapshot = item.as_ref().and_then(item_price_snapshot);
    }

    // v1.1/v1.1.3 — consume the promo (the gate). A claim refusal degrades to
    // full price (drop discount, emit signal), never bricks a paid order.
    let mut offer_id: Option<String> = None;
    let mut promo_claimed = false;
    match claim_entitlement(loyalty, &order, item.as_ref()).await {
        Ok(oid) => {
            promo_claimed = oid.is_some();
            offer_id = oid;
        }
        Err(e) => {
            tracing::warn!(commercial_order_id = order.id, customer_id = p.customer_id, error = %e, "order.promo.claim_failed_degrade_to_full_price");
            stage(
                conn,
                ctx,
                "order.promo_not_applied",
                "ProductOrder",
                &order.id,
                json!({
                    "commercialOrderId": order.id,
                    "customerId": p.customer_id,
                    "promoCode": item.as_ref().and_then(|i| i.discount_code.clone()),
                    "reason": "claim_refused",
                }),
            )
            .await?;
        }
    }

    if promo_claimed {
        if let (Some(it), Some(ps)) = (item.as_ref(), price_snapshot.as_mut()) {
            ps["discountType"] = json!(it.discount_type);
            ps["discountValue"] = json!(it.discount_value.map(|d| d.to_string()));
            ps["discountPeriodsTotal"] = json!(it.discount_periods_total);
            ps["promoCode"] = json!(it.discount_code);
            ps["promoOfferDefinitionId"] = json!(it.promo_offer_definition_id);
        }
    }

    // Create subscription (idempotent on commercialOrderId).
    let mut body = json!({
        "customerId": p.customer_id,
        "offeringId": p.offering_id,
        "msisdn": p.msisdn,
        "iccid": p.iccid,
        "paymentMethodId": p.payment_method_id,
        "commercialOrderId": p.commercial_order_id,
    });
    if let Some(ps) = &price_snapshot {
        body["priceSnapshot"] = ps.clone();
    }
    let sub_result = match subscription.create(&body).await {
        Ok(v) => v,
        Err(e) => {
            // Activation failed (typically a payment decline). Release the
            // entitlement so a single-use code isn't burned, then propagate.
            if let (Some(oid), Some(loyalty)) = (&offer_id, loyalty) {
                revoke_entitlement(loyalty, oid, &order.id).await;
            }
            return Err(upstream(e));
        }
    };
    let subscription_id = sub_result
        .get("id")
        .and_then(Value::as_str)
        .map(str::to_string);

    // Activation succeeded → redeem.
    if let (Some(oid), Some(loyalty)) = (&offer_id, loyalty) {
        redeem_entitlement(loyalty, oid, &order.id).await;
    }

    // Update order item.
    if let Some(it) = &item {
        sqlx::query(
            "UPDATE order_mgmt.order_item SET target_subscription_id=$2, state='completed', \
             promo_offer_id=COALESCE($3, promo_offer_id), updated_at=now() WHERE id=$1",
        )
        .bind(&it.id)
        .bind(&subscription_id)
        .bind(&offer_id)
        .execute(&mut *conn)
        .await?;
    }

    check_order_transition(&order.state, "completed")?;
    let now = bss_clock::now();
    sqlx::query("UPDATE order_mgmt.product_order SET state='completed', completed_date=$2, updated_at=now() WHERE id=$1")
        .bind(&order.id)
        .bind(now)
        .execute(&mut *conn)
        .await?;
    add_state_history(
        conn,
        &order.id,
        Some("in_progress"),
        Some("completed"),
        ctx,
        "service order completed",
    )
    .await?;

    stage(
        conn,
        ctx,
        "order.completed",
        "ProductOrder",
        &order.id,
        json!({
            "commercialOrderId": order.id,
            "customerId": order.customer_id,
            "subscriptionId": subscription_id,
            "cfsServiceId": p.cfs_service_id,
        }),
    )
    .await?;
    Ok(())
}

pub async fn handle_service_order_failed(
    conn: &mut PgConnection,
    ctx: &RequestCtx,
    commercial_order_id: &str,
    reason: &str,
) -> Result<(), ApiError> {
    let Some((order, item)) = repo::get_for_update(conn, commercial_order_id).await? else {
        tracing::warn!(
            commercial_order_id,
            reason = "order not found or not in_progress",
            "order.service_order_failed.skipped"
        );
        return Ok(());
    };
    if order.state != "in_progress" {
        tracing::warn!(
            commercial_order_id,
            reason = "order not found or not in_progress",
            "order.service_order_failed.skipped"
        );
        return Ok(());
    }

    check_order_transition(&order.state, "failed")?;
    let now = bss_clock::now();
    sqlx::query("UPDATE order_mgmt.product_order SET state='failed', completed_date=$2, updated_at=now() WHERE id=$1")
        .bind(&order.id)
        .bind(now)
        .execute(&mut *conn)
        .await?;
    if let Some(it) = &item {
        sqlx::query(
            "UPDATE order_mgmt.order_item SET state='failed', updated_at=now() WHERE id=$1",
        )
        .bind(&it.id)
        .execute(&mut *conn)
        .await?;
    }
    add_state_history(
        conn,
        &order.id,
        Some("in_progress"),
        Some("failed"),
        ctx,
        reason,
    )
    .await?;
    stage(
        conn,
        ctx,
        "order.failed",
        "ProductOrder",
        &order.id,
        json!({ "commercialOrderId": order.id, "customerId": order.customer_id, "reason": reason }),
    )
    .await?;
    Ok(())
}

// ── promo consume helpers ─────────────────────────────────────────────────────

/// Consume the promo entitlement at activation. Targeted (`promo_offer_id` set) →
/// `advance_to_claimed`; public typed → mint-and-claim by code. `Ok(None)` when
/// there's no promo. Returns the loyalty offer id to redeem/revoke.
async fn claim_entitlement(
    loyalty: Option<&LoyaltyClient>,
    order: &OrderRow,
    item: Option<&ItemRow>,
) -> Result<Option<String>, ClientError> {
    let Some(item) = item else { return Ok(None) };
    if item.discount_type.is_none() || item.discount_code.is_none() {
        return Ok(None);
    }
    let Some(loyalty) = loyalty else {
        return Ok(None);
    };

    if let Some(pre_offer) = &item.promo_offer_id {
        // Targeted path: pre-paired offer → advance it.
        loyalty
            .advance_offer_to_claimed(pre_offer, &format!("{}:claim", order.id), Some(&order.id))
            .await?;
        return Ok(Some(pre_offer.clone()));
    }
    // Public typed path: mint-and-claim by code.
    let code = item.discount_code.as_deref().unwrap_or("");
    let result = loyalty
        .claim_offer(
            &order.customer_id,
            json!({ "type": "promo_code", "code": code }),
            &format!("{}:claim", order.id),
        )
        .await?;
    Ok(result
        .get("offer_id")
        .and_then(Value::as_str)
        .map(str::to_string))
}

/// Finalize the entitlement after a successful activation. Best-effort.
async fn redeem_entitlement(loyalty: &LoyaltyClient, offer_id: &str, order_id: &str) {
    if let Err(e) = loyalty
        .redeem_offer(offer_id, order_id, &format!("{order_id}:redeem"))
        .await
    {
        tracing::warn!(offer_id, error = %e, "order.promo.redeem_failed");
    }
}

/// Release the entitlement when activation fails. Best-effort.
async fn revoke_entitlement(loyalty: &LoyaltyClient, offer_id: &str, order_id: &str) {
    if let Err(e) = loyalty
        .revoke_offer(
            offer_id,
            bss_clients::REVOKE_ORDER_CANCELLED,
            &format!("{order_id}:revoke"),
        )
        .await
    {
        tracing::warn!(offer_id, error = %e, "order.promo.revoke_failed");
    }
}

// ── small helpers ─────────────────────────────────────────────────────────────

/// The price-snapshot object built off an order item row (the durable source of
/// truth when the event arrives stripped). `priceAmount` renders the DB decimal.
fn item_price_snapshot(item: &ItemRow) -> Option<Value> {
    item.price_amount.map(|amount| {
        json!({
            "priceAmount": amount.to_string(),
            "priceCurrency": item.price_currency,
            "priceOfferingPriceId": item.price_offering_price_id,
        })
    })
}

/// `str(value)` for a money field that is a JSON float on the wire — matches
/// Python's `Decimal(str(...))` seed string (`25.0`, not `25`).
fn money_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

async fn add_state_history(
    conn: &mut PgConnection,
    order_id: &str,
    from_state: Option<&str>,
    to_state: Option<&str>,
    ctx: &RequestCtx,
    reason: &str,
) -> Result<(), ApiError> {
    let now = bss_clock::now();
    sqlx::query(
        "INSERT INTO order_mgmt.order_state_history \
         (order_id, from_state, to_state, changed_by, reason, event_time) \
         VALUES ($1,$2,$3,$4,$5,$6)",
    )
    .bind(order_id)
    .bind(from_state)
    .bind(to_state)
    .bind(&ctx.actor)
    .bind(reason)
    .bind(now)
    .execute(conn)
    .await?;
    Ok(())
}

fn order_not_found(order_id: &str) -> ApiError {
    PolicyViolation::with_context(
        "order.not_found",
        format!("Order {order_id} not found"),
        json!({ "order_id": order_id }),
    )
    .into()
}

fn upstream(e: ClientError) -> ApiError {
    ApiError::Internal(format!("upstream error: {e}"))
}
