//! `order_mgmt` persistence — port of `OrderRepository`.
//!
//! HTTP reads run on the pool; consumer-path writes run on the safe consumer's
//! `&mut PgConnection` (so the inbox claim + all writes commit atomically).
//! Money columns are read as `::text` → `Decimal` (2dp scale preserved; the wire
//! renders `priceAmount`/`discountValue` as strings). The order aggregate is read
//! `FOR UPDATE` on the consumer path (the SOM P2 lesson: serialize the
//! multi-event read-modify-write on the shared aggregate).

use std::str::FromStr;

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use sqlx::postgres::{PgConnection, PgRow};
use sqlx::{PgPool, Row};

use crate::error::ApiError;

#[derive(Debug, Clone)]
pub struct OrderRow {
    pub id: String,
    pub customer_id: String,
    pub state: String,
    pub order_date: Option<DateTime<Utc>>,
    pub requested_completion_date: Option<DateTime<Utc>>,
    pub completed_date: Option<DateTime<Utc>>,
    pub msisdn_preference: Option<String>,
    pub notes: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ItemRow {
    pub id: String,
    pub action: String,
    pub offering_id: String,
    pub state: Option<String>,
    pub target_subscription_id: Option<String>,
    pub price_amount: Option<Decimal>,
    pub price_currency: Option<String>,
    pub price_offering_price_id: Option<String>,
    pub discount_code: Option<String>,
    pub promo_offer_definition_id: Option<String>,
    pub discount_type: Option<String>,
    pub discount_value: Option<Decimal>,
    pub discount_periods_total: Option<i16>,
    pub promo_offer_id: Option<String>,
}

#[derive(Debug, Clone)]
pub struct OrderFull {
    pub order: OrderRow,
    pub items: Vec<ItemRow>,
}

const ORDER_COLS: &str = "id, customer_id, state, order_date, requested_completion_date, \
     completed_date, msisdn_preference, notes";
const ITEM_COLS: &str = "id, action, offering_id, state, target_subscription_id, \
     price_amount::text AS price_amount, price_currency, price_offering_price_id, discount_code, \
     promo_offer_definition_id, discount_type, discount_value::text AS discount_value, \
     discount_periods_total, promo_offer_id";

fn opt_decimal(row: &PgRow, col: &str) -> Result<Option<Decimal>, ApiError> {
    let text: Option<String> = row.try_get(col)?;
    match text {
        Some(t) => Decimal::from_str(&t)
            .map(Some)
            .map_err(|e| ApiError::Internal(format!("bad decimal in {col}: {e}"))),
        None => Ok(None),
    }
}

fn order_from_row(row: &PgRow) -> Result<OrderRow, ApiError> {
    Ok(OrderRow {
        id: row.try_get("id")?,
        customer_id: row.try_get("customer_id")?,
        state: row.try_get("state")?,
        order_date: row.try_get("order_date")?,
        requested_completion_date: row.try_get("requested_completion_date")?,
        completed_date: row.try_get("completed_date")?,
        msisdn_preference: row.try_get("msisdn_preference")?,
        notes: row.try_get("notes")?,
    })
}

fn item_from_row(row: &PgRow) -> Result<ItemRow, ApiError> {
    Ok(ItemRow {
        id: row.try_get("id")?,
        action: row.try_get("action")?,
        offering_id: row.try_get("offering_id")?,
        state: row.try_get("state")?,
        target_subscription_id: row.try_get("target_subscription_id")?,
        price_amount: opt_decimal(row, "price_amount")?,
        price_currency: row.try_get("price_currency")?,
        price_offering_price_id: row.try_get("price_offering_price_id")?,
        discount_code: row.try_get("discount_code")?,
        promo_offer_definition_id: row.try_get("promo_offer_definition_id")?,
        discount_type: row.try_get("discount_type")?,
        discount_value: opt_decimal(row, "discount_value")?,
        discount_periods_total: row.try_get("discount_periods_total")?,
        promo_offer_id: row.try_get("promo_offer_id")?,
    })
}

// ── id sequences ──────────────────────────────────────────────────────────────

pub async fn next_order_id(pool: &PgPool) -> Result<String, ApiError> {
    let n: i64 = sqlx::query_scalar("SELECT nextval('order_mgmt.product_order_id_seq')")
        .fetch_one(pool)
        .await?;
    Ok(format!("ORD-{n:04}"))
}

pub async fn next_item_id(pool: &PgPool) -> Result<String, ApiError> {
    let n: i64 = sqlx::query_scalar("SELECT nextval('order_mgmt.order_item_id_seq')")
        .fetch_one(pool)
        .await?;
    Ok(format!("OI-{n:04}"))
}

// ── reads ─────────────────────────────────────────────────────────────────────

async fn items_for(pool: &PgPool, order_id: &str) -> Result<Vec<ItemRow>, ApiError> {
    let rows = sqlx::query(&format!(
        "SELECT {ITEM_COLS} FROM order_mgmt.order_item WHERE order_id = $1 ORDER BY id"
    ))
    .bind(order_id)
    .fetch_all(pool)
    .await?;
    rows.iter().map(item_from_row).collect()
}

pub async fn get(pool: &PgPool, order_id: &str) -> Result<Option<OrderFull>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {ORDER_COLS} FROM order_mgmt.product_order WHERE id = $1"
    ))
    .bind(order_id)
    .fetch_optional(pool)
    .await?;
    match row {
        Some(r) => {
            let order = order_from_row(&r)?;
            let items = items_for(pool, order_id).await?;
            Ok(Some(OrderFull { order, items }))
        }
        None => Ok(None),
    }
}

/// List orders newest-first, optionally filtered. Reads are free (motto #7).
pub async fn list(
    pool: &PgPool,
    customer_id: Option<&str>,
    state: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<OrderFull>, ApiError> {
    let rows = match (customer_id, state) {
        (Some(c), Some(s)) => {
            sqlx::query(&format!(
            "SELECT {ORDER_COLS} FROM order_mgmt.product_order WHERE customer_id=$1 AND state=$2 \
             ORDER BY created_at DESC LIMIT $3 OFFSET $4"
        ))
            .bind(c)
            .bind(s)
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
        (Some(c), None) => {
            sqlx::query(&format!(
                "SELECT {ORDER_COLS} FROM order_mgmt.product_order WHERE customer_id=$1 \
             ORDER BY created_at DESC LIMIT $2 OFFSET $3"
            ))
            .bind(c)
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
        (None, Some(s)) => {
            sqlx::query(&format!(
                "SELECT {ORDER_COLS} FROM order_mgmt.product_order WHERE state=$1 \
             ORDER BY created_at DESC LIMIT $2 OFFSET $3"
            ))
            .bind(s)
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
        (None, None) => {
            sqlx::query(&format!(
                "SELECT {ORDER_COLS} FROM order_mgmt.product_order \
             ORDER BY created_at DESC LIMIT $1 OFFSET $2"
            ))
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
    };
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let order = order_from_row(r)?;
        let items = items_for(pool, &order.id).await?;
        out.push(OrderFull { order, items });
    }
    Ok(out)
}

// ── consumer-path reads/writes (on the tx connection) ─────────────────────────

/// Read an order + its first item `FOR UPDATE` on the consumer's connection
/// (serialize the multi-event RMW — the SOM P2 lesson applied to com).
pub async fn get_for_update(
    conn: &mut PgConnection,
    order_id: &str,
) -> Result<Option<(OrderRow, Option<ItemRow>)>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {ORDER_COLS} FROM order_mgmt.product_order WHERE id = $1 FOR UPDATE"
    ))
    .bind(order_id)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(r) = row else { return Ok(None) };
    let order = order_from_row(&r)?;
    let item_row = sqlx::query(&format!(
        "SELECT {ITEM_COLS} FROM order_mgmt.order_item WHERE order_id = $1 ORDER BY id LIMIT 1"
    ))
    .bind(order_id)
    .fetch_optional(&mut *conn)
    .await?;
    let item = item_row.as_ref().map(item_from_row).transpose()?;
    Ok(Some((order, item)))
}
