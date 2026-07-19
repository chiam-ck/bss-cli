//! `subscription` schema persistence — port of `SubscriptionRepository` +
//! `VasPurchaseRepository`.
//!
//! HTTP reads run on the pool; the consumer- and worker-path reads/writes run on a
//! `&mut PgConnection` (a transaction) so the `SELECT ... FOR UPDATE` decrement and
//! the mark-before-dispatch sweep commit atomically. Money columns (`price_amount`,
//! `discount_value`) are read as `::text` → `Decimal` (2dp scale preserved; the
//! wire renders them as strings). Parsers + column lists are shared so the pool and
//! connection paths agree byte-for-byte.

use std::str::FromStr;

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use sqlx::postgres::{PgConnection, PgRow};
use sqlx::{PgPool, Row};

use crate::error::ApiError;

#[derive(Debug, Clone)]
pub struct SubscriptionRow {
    pub id: String,
    pub customer_id: String,
    pub offering_id: String,
    pub commercial_order_id: Option<String>,
    pub msisdn: String,
    pub iccid: String,
    pub cfs_service_id: Option<String>,
    pub state: String,
    pub state_reason: Option<String>,
    pub activated_at: Option<DateTime<Utc>>,
    pub current_period_start: Option<DateTime<Utc>>,
    pub current_period_end: Option<DateTime<Utc>>,
    pub next_renewal_at: Option<DateTime<Utc>>,
    pub terminated_at: Option<DateTime<Utc>>,
    pub price_amount: Decimal,
    pub price_currency: String,
    pub price_offering_price_id: String,
    pub pending_offering_id: Option<String>,
    pub pending_offering_price_id: Option<String>,
    pub pending_effective_at: Option<DateTime<Utc>>,
    pub discount_type: Option<String>,
    pub discount_value: Option<Decimal>,
    pub discount_periods_remaining: i16,
    pub promo_code: Option<String>,
    pub promo_offer_definition_id: Option<String>,
}

#[derive(Debug, Clone)]
pub struct BundleBalanceRow {
    pub id: String,
    pub subscription_id: String,
    pub allowance_type: String,
    pub total: i64,
    pub consumed: i64,
    pub unit: String,
    pub period_start: Option<DateTime<Utc>>,
    pub period_end: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone)]
pub struct SubscriptionFull {
    pub sub: SubscriptionRow,
    pub balances: Vec<BundleBalanceRow>,
}

pub const SUB_COLS: &str = "id, customer_id, offering_id, commercial_order_id, msisdn, iccid, \
     cfs_service_id, state, state_reason, activated_at, current_period_start, current_period_end, \
     next_renewal_at, terminated_at, price_amount::text AS price_amount, price_currency, \
     price_offering_price_id, pending_offering_id, pending_offering_price_id, pending_effective_at, \
     discount_type, discount_value::text AS discount_value, discount_periods_remaining, promo_code, \
     promo_offer_definition_id";

pub const BAL_COLS: &str = "id, subscription_id, allowance_type, total, consumed, unit, \
     period_start, period_end";

fn decimal(row: &PgRow, col: &str) -> Result<Decimal, ApiError> {
    let t: String = row.try_get(col)?;
    Decimal::from_str(&t).map_err(|e| ApiError::Internal(format!("bad decimal in {col}: {e}")))
}

fn opt_decimal(row: &PgRow, col: &str) -> Result<Option<Decimal>, ApiError> {
    let t: Option<String> = row.try_get(col)?;
    match t {
        Some(s) => Decimal::from_str(&s)
            .map(Some)
            .map_err(|e| ApiError::Internal(format!("bad decimal in {col}: {e}"))),
        None => Ok(None),
    }
}

pub fn sub_from_row(row: &PgRow) -> Result<SubscriptionRow, ApiError> {
    Ok(SubscriptionRow {
        id: row.try_get("id")?,
        customer_id: row.try_get("customer_id")?,
        offering_id: row.try_get("offering_id")?,
        commercial_order_id: row.try_get("commercial_order_id")?,
        msisdn: row.try_get("msisdn")?,
        iccid: row.try_get("iccid")?,
        cfs_service_id: row.try_get("cfs_service_id")?,
        state: row.try_get("state")?,
        state_reason: row.try_get("state_reason")?,
        activated_at: row.try_get("activated_at")?,
        current_period_start: row.try_get("current_period_start")?,
        current_period_end: row.try_get("current_period_end")?,
        next_renewal_at: row.try_get("next_renewal_at")?,
        terminated_at: row.try_get("terminated_at")?,
        price_amount: decimal(row, "price_amount")?,
        price_currency: row.try_get("price_currency")?,
        price_offering_price_id: row.try_get("price_offering_price_id")?,
        pending_offering_id: row.try_get("pending_offering_id")?,
        pending_offering_price_id: row.try_get("pending_offering_price_id")?,
        pending_effective_at: row.try_get("pending_effective_at")?,
        discount_type: row.try_get("discount_type")?,
        discount_value: opt_decimal(row, "discount_value")?,
        discount_periods_remaining: row.try_get("discount_periods_remaining")?,
        promo_code: row.try_get("promo_code")?,
        promo_offer_definition_id: row.try_get("promo_offer_definition_id")?,
    })
}

pub fn balance_from_row(row: &PgRow) -> Result<BundleBalanceRow, ApiError> {
    Ok(BundleBalanceRow {
        id: row.try_get("id")?,
        subscription_id: row.try_get("subscription_id")?,
        allowance_type: row.try_get("allowance_type")?,
        total: row.try_get("total")?,
        consumed: row.try_get("consumed")?,
        unit: row.try_get("unit")?,
        period_start: row.try_get("period_start")?,
        period_end: row.try_get("period_end")?,
    })
}

// ── id sequence ─────────────────────────────────────────────────────────────

pub async fn next_id(pool: &PgPool) -> Result<String, ApiError> {
    let n: i64 = sqlx::query_scalar("SELECT nextval('subscription.subscription_id_seq')")
        .fetch_one(pool)
        .await?;
    Ok(format!("SUB-{n:04}"))
}

// ── HTTP reads (on the pool) ────────────────────────────────────────────────

pub async fn get_balances(pool: &PgPool, sub_id: &str) -> Result<Vec<BundleBalanceRow>, ApiError> {
    // No ORDER BY — the oracle's `get_balances` / selectinload have none, so the
    // wire order is the table's physical (insertion) order: DATA, VOICE, SMS,
    // DATA_ROAMING. Both services read the same quiescent heap → byte-identical.
    let rows = sqlx::query(&format!(
        "SELECT {BAL_COLS} FROM subscription.bundle_balance WHERE subscription_id = $1"
    ))
    .bind(sub_id)
    .fetch_all(pool)
    .await?;
    rows.iter().map(balance_from_row).collect()
}

async fn hydrate(pool: &PgPool, row: &PgRow) -> Result<SubscriptionFull, ApiError> {
    let sub = sub_from_row(row)?;
    let balances = get_balances(pool, &sub.id).await?;
    Ok(SubscriptionFull { sub, balances })
}

pub async fn get(pool: &PgPool, sub_id: &str) -> Result<Option<SubscriptionFull>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {SUB_COLS} FROM subscription.subscription WHERE id = $1"
    ))
    .bind(sub_id)
    .fetch_optional(pool)
    .await?;
    match row {
        Some(r) => Ok(Some(hydrate(pool, &r).await?)),
        None => Ok(None),
    }
}

pub async fn get_by_msisdn(
    pool: &PgPool,
    msisdn: &str,
) -> Result<Option<SubscriptionFull>, ApiError> {
    // Python filters only on msisdn (the partial unique index guarantees at most
    // one non-terminated row; a terminated one can also match — mirror exactly and
    // take the first).
    let row = sqlx::query(&format!(
        "SELECT {SUB_COLS} FROM subscription.subscription WHERE msisdn = $1 LIMIT 1"
    ))
    .bind(msisdn)
    .fetch_optional(pool)
    .await?;
    match row {
        Some(r) => Ok(Some(hydrate(pool, &r).await?)),
        None => Ok(None),
    }
}

pub async fn get_by_commercial_order(
    pool: &PgPool,
    order_id: &str,
) -> Result<Option<SubscriptionFull>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {SUB_COLS} FROM subscription.subscription WHERE commercial_order_id = $1 LIMIT 1"
    ))
    .bind(order_id)
    .fetch_optional(pool)
    .await?;
    match row {
        Some(r) => Ok(Some(hydrate(pool, &r).await?)),
        None => Ok(None),
    }
}

pub async fn list_for_customer(
    pool: &PgPool,
    customer_id: &str,
) -> Result<Vec<SubscriptionFull>, ApiError> {
    let rows = sqlx::query(&format!(
        "SELECT {SUB_COLS} FROM subscription.subscription WHERE customer_id = $1 \
         ORDER BY created_at DESC"
    ))
    .bind(customer_id)
    .fetch_all(pool)
    .await?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        out.push(hydrate(pool, r).await?);
    }
    Ok(out)
}

/// Subscriptions on `offering_id` still in a renewable state (`active`|`blocked`),
/// ordered by id — port of `list_active_for_offering` (used by price migration).
pub async fn list_active_for_offering(
    pool: &PgPool,
    offering_id: &str,
) -> Result<Vec<SubscriptionRow>, ApiError> {
    let rows = sqlx::query(&format!(
        "SELECT {SUB_COLS} FROM subscription.subscription \
         WHERE offering_id = $1 AND state IN ('active','blocked') ORDER BY id"
    ))
    .bind(offering_id)
    .fetch_all(pool)
    .await?;
    rows.iter().map(sub_from_row).collect()
}

// ── consumer-path reads (on the tx connection) ──────────────────────────────

pub async fn get_sub_on_conn(
    conn: &mut PgConnection,
    sub_id: &str,
) -> Result<Option<SubscriptionRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {SUB_COLS} FROM subscription.subscription WHERE id = $1"
    ))
    .bind(sub_id)
    .fetch_optional(&mut *conn)
    .await?;
    row.as_ref().map(sub_from_row).transpose()
}

/// `SELECT ... FOR UPDATE` on the balance row. Serializes concurrent decrement
/// attempts per (subscription, allowance). In sqlx each query hits Postgres
/// directly (no identity-map cache), so the value read here is always the latest
/// committed — the Python `populate_existing=True` fix is structurally free.
pub async fn get_balance_for_update(
    conn: &mut PgConnection,
    sub_id: &str,
    allowance_type: &str,
) -> Result<Option<BundleBalanceRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {BAL_COLS} FROM subscription.bundle_balance \
         WHERE subscription_id = $1 AND allowance_type = $2 FOR UPDATE"
    ))
    .bind(sub_id)
    .bind(allowance_type)
    .fetch_optional(&mut *conn)
    .await?;
    row.as_ref().map(balance_from_row).transpose()
}

pub async fn get_balances_on_conn(
    conn: &mut PgConnection,
    sub_id: &str,
) -> Result<Vec<BundleBalanceRow>, ApiError> {
    let rows = sqlx::query(&format!(
        "SELECT {BAL_COLS} FROM subscription.bundle_balance WHERE subscription_id = $1"
    ))
    .bind(sub_id)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter().map(balance_from_row).collect()
}

// ── v0.18 renewal-worker sweeps (on the tx connection) ──────────────────────
//
// `FOR UPDATE SKIP LOCKED` so a peer replica grabs disjoint rows; the caller
// commits the marking UPDATE inside the SAME transaction so the dedup column is
// visible the moment the row lock releases (mark-before-dispatch).

pub async fn due_for_renewal(
    conn: &mut PgConnection,
    now: DateTime<Utc>,
    limit: i64,
    tenant_id: &str,
) -> Result<Vec<String>, ApiError> {
    let rows = sqlx::query(
        "SELECT id FROM subscription.subscription \
         WHERE state = 'active' AND next_renewal_at IS NOT NULL AND next_renewal_at <= $1 \
           AND (last_renewal_attempted_at IS NULL OR last_renewal_attempted_at < next_renewal_at) \
           AND tenant_id = $3 \
         ORDER BY next_renewal_at LIMIT $2 FOR UPDATE SKIP LOCKED",
    )
    .bind(now)
    .bind(limit)
    .bind(tenant_id)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|r| r.try_get("id").map_err(Into::into))
        .collect()
}

pub async fn overdue_blocked(
    conn: &mut PgConnection,
    now: DateTime<Utc>,
    limit: i64,
    tenant_id: &str,
) -> Result<Vec<String>, ApiError> {
    let rows = sqlx::query(
        "SELECT id FROM subscription.subscription \
         WHERE state = 'blocked' AND next_renewal_at IS NOT NULL AND next_renewal_at <= $1 \
           AND (last_renewal_attempted_at IS NULL OR last_renewal_attempted_at < next_renewal_at) \
           AND tenant_id = $3 \
         ORDER BY next_renewal_at LIMIT $2 FOR UPDATE SKIP LOCKED",
    )
    .bind(now)
    .bind(limit)
    .bind(tenant_id)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|r| r.try_get("id").map_err(Into::into))
        .collect()
}

pub async fn mark_renewal_attempted(
    conn: &mut PgConnection,
    ids: &[String],
    at: DateTime<Utc>,
) -> Result<(), ApiError> {
    if ids.is_empty() {
        return Ok(());
    }
    sqlx::query(
        "UPDATE subscription.subscription SET last_renewal_attempted_at = $1, updated_at = $1 \
         WHERE id = ANY($2)",
    )
    .bind(at)
    .bind(ids)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

#[derive(Debug, Clone)]
pub struct ReminderRow {
    pub id: String,
    pub customer_id: String,
    pub offering_id: String,
    pub msisdn: String,
    pub next_renewal_at: Option<DateTime<Utc>>,
    pub price_amount: Decimal,
    pub price_currency: String,
}

pub async fn due_for_reminder(
    conn: &mut PgConnection,
    now: DateTime<Utc>,
    lookahead_seconds: i64,
    limit: i64,
    tenant_id: &str,
) -> Result<Vec<ReminderRow>, ApiError> {
    let rows = sqlx::query(
        "SELECT id, customer_id, offering_id, msisdn, next_renewal_at, \
                price_amount::text AS price_amount, price_currency \
         FROM subscription.subscription \
         WHERE state = 'active' AND next_renewal_at IS NOT NULL AND next_renewal_at > $1 \
           AND next_renewal_at <= $1 + make_interval(secs => $2::double precision) \
           AND (renewal_reminder_sent_at IS NULL OR renewal_reminder_sent_at < next_renewal_at) \
           AND tenant_id = $4 \
         ORDER BY next_renewal_at LIMIT $3 FOR UPDATE SKIP LOCKED",
    )
    .bind(now)
    .bind(lookahead_seconds as f64)
    .bind(limit)
    .bind(tenant_id)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|r| {
            Ok(ReminderRow {
                id: r.try_get("id")?,
                customer_id: r.try_get("customer_id")?,
                offering_id: r.try_get("offering_id")?,
                msisdn: r.try_get("msisdn")?,
                next_renewal_at: r.try_get("next_renewal_at")?,
                price_amount: decimal(r, "price_amount")?,
                price_currency: r.try_get("price_currency")?,
            })
        })
        .collect()
}

pub async fn mark_reminder_sent(
    conn: &mut PgConnection,
    ids: &[String],
    at: DateTime<Utc>,
) -> Result<(), ApiError> {
    if ids.is_empty() {
        return Ok(());
    }
    sqlx::query(
        "UPDATE subscription.subscription SET renewal_reminder_sent_at = $1, updated_at = $1 \
         WHERE id = ANY($2)",
    )
    .bind(at)
    .bind(ids)
    .execute(&mut *conn)
    .await?;
    Ok(())
}
