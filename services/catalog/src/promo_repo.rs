//! Promotion reads + eligibility writes — port of `PromotionRepository`.
//!
//! Reads run on the pool; eligibility inserts/deletes run on the service's
//! transaction connection (so assign/unassign commit atomically). `discount_value`
//! is read as text → `Decimal` to preserve the 2dp scale the wire needs as a
//! string (`"20.00"`).

use std::str::FromStr;

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use sqlx::postgres::PgRow;
use sqlx::{PgConnection, PgPool, Row};

use crate::error::ApiError;

#[derive(Debug, Clone)]
pub struct PromotionRow {
    pub id: String,
    pub code: Option<String>,
    pub name: Option<String>,
    pub audience: String,
    pub offer_definition_id: Option<String>,
    pub discount_type: String,
    pub discount_value: Decimal,
    pub currency: String,
    pub applicable_offering_ids: Option<Vec<String>>,
    pub duration_kind: String,
    pub periods_total: Option<i16>,
    pub valid_from: Option<DateTime<Utc>>,
    pub valid_to: Option<DateTime<Utc>>,
    pub state: String,
    pub created_by: String,
}

const COLS: &str = "id, code, name, audience, offer_definition_id, discount_type, \
     discount_value::text AS discount_value, currency, applicable_offering_ids, duration_kind, \
     periods_total, valid_from, valid_to, state, created_by";

fn from_row(row: &PgRow) -> Result<PromotionRow, ApiError> {
    let dv: String = row.try_get("discount_value")?;
    Ok(PromotionRow {
        id: row.try_get("id")?,
        code: row.try_get("code")?,
        name: row.try_get("name")?,
        audience: row.try_get("audience")?,
        offer_definition_id: row.try_get("offer_definition_id")?,
        discount_type: row.try_get("discount_type")?,
        discount_value: Decimal::from_str(&dv)
            .map_err(|e| ApiError::Internal(format!("bad discount_value: {e}")))?,
        currency: row.try_get("currency")?,
        applicable_offering_ids: row.try_get("applicable_offering_ids")?,
        duration_kind: row.try_get("duration_kind")?,
        periods_total: row.try_get("periods_total")?,
        valid_from: row.try_get("valid_from")?,
        valid_to: row.try_get("valid_to")?,
        state: row.try_get("state")?,
        created_by: row.try_get("created_by")?,
    })
}

pub async fn get(pool: &PgPool, promotion_id: &str) -> Result<Option<PromotionRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {COLS} FROM catalog.promotion WHERE id = $1"
    ))
    .bind(promotion_id)
    .fetch_optional(pool)
    .await?;
    row.as_ref().map(from_row).transpose()
}

pub async fn get_by_code(pool: &PgPool, code: &str) -> Result<Option<PromotionRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {COLS} FROM catalog.promotion WHERE code = $1"
    ))
    .bind(code)
    .fetch_optional(pool)
    .await?;
    row.as_ref().map(from_row).transpose()
}

pub async fn get_by_offer_definition_id(
    pool: &PgPool,
    od_id: &str,
) -> Result<Option<PromotionRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {COLS} FROM catalog.promotion WHERE offer_definition_id = $1"
    ))
    .bind(od_id)
    .fetch_optional(pool)
    .await?;
    row.as_ref().map(from_row).transpose()
}

pub async fn list(
    pool: &PgPool,
    state: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<PromotionRow>, ApiError> {
    let rows = match state {
        Some(s) => {
            sqlx::query(&format!(
            "SELECT {COLS} FROM catalog.promotion WHERE state = $1 ORDER BY id LIMIT $2 OFFSET $3"
        ))
            .bind(s)
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
        None => {
            sqlx::query(&format!(
                "SELECT {COLS} FROM catalog.promotion ORDER BY id LIMIT $1 OFFSET $2"
            ))
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
    };
    rows.iter().map(from_row).collect()
}

pub async fn is_eligible(
    pool: &PgPool,
    promotion_id: &str,
    customer_id: &str,
) -> Result<bool, ApiError> {
    let row = sqlx::query(
        "SELECT 1 FROM catalog.promotion_eligibility WHERE promotion_id = $1 AND customer_id = $2 LIMIT 1",
    )
    .bind(promotion_id)
    .bind(customer_id)
    .fetch_optional(pool)
    .await?;
    Ok(row.is_some())
}

/// Eligibility check on the service's transaction connection — sees the
/// uncommitted inserts of the same assign batch (matching SQLAlchemy autoflush),
/// so a customer listed twice in one call is reported as `already` the second time.
pub async fn is_eligible_on(
    conn: &mut PgConnection,
    promotion_id: &str,
    customer_id: &str,
) -> Result<bool, ApiError> {
    let row = sqlx::query(
        "SELECT 1 FROM catalog.promotion_eligibility WHERE promotion_id = $1 AND customer_id = $2 LIMIT 1",
    )
    .bind(promotion_id)
    .bind(customer_id)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.is_some())
}

/// Upfront-minted loyalty offer id for (promo, customer). `None` = no row or a
/// pre-v1.3.0 NULL (consume then falls back to claim-by-code).
pub async fn get_loyalty_offer_id(
    pool: &PgPool,
    promotion_id: &str,
    customer_id: &str,
) -> Result<Option<String>, ApiError> {
    let row = sqlx::query(
        "SELECT loyalty_offer_id FROM catalog.promotion_eligibility \
         WHERE promotion_id = $1 AND customer_id = $2",
    )
    .bind(promotion_id)
    .bind(customer_id)
    .fetch_optional(pool)
    .await?;
    match row {
        Some(r) => Ok(r.try_get::<Option<String>, _>("loyalty_offer_id")?),
        None => Ok(None),
    }
}

pub async fn list_eligible_promotions(
    pool: &PgPool,
    customer_id: &str,
) -> Result<Vec<PromotionRow>, ApiError> {
    // Same columns, p.-prefixed for the join.
    let cols_p = COLS
        .split(", ")
        .map(|c| {
            if let Some(rest) = c.strip_suffix(" AS discount_value") {
                format!("p.{rest} AS discount_value")
            } else {
                format!("p.{c}")
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    let rows = sqlx::query(&format!(
        "SELECT {cols_p} FROM catalog.promotion p \
         JOIN catalog.promotion_eligibility e ON e.promotion_id = p.id \
         WHERE e.customer_id = $1 AND p.state = 'active' AND p.audience = 'targeted' \
         ORDER BY p.id"
    ))
    .bind(customer_id)
    .fetch_all(pool)
    .await?;
    rows.iter().map(from_row).collect()
}

// ── eligibility writes (on the service's tx connection) ───────────────────────

/// Add a (promotion, customer) eligibility row. Idempotent — `Ok(false)` if it
/// already existed.
pub async fn add_eligibility(
    conn: &mut PgConnection,
    promotion_id: &str,
    customer_id: &str,
    created_by: &str,
    loyalty_offer_id: Option<&str>,
) -> Result<bool, ApiError> {
    let existing = sqlx::query(
        "SELECT id FROM catalog.promotion_eligibility WHERE promotion_id = $1 AND customer_id = $2",
    )
    .bind(promotion_id)
    .bind(customer_id)
    .fetch_optional(&mut *conn)
    .await?;
    if existing.is_some() {
        return Ok(false);
    }
    sqlx::query(
        "INSERT INTO catalog.promotion_eligibility \
         (promotion_id, customer_id, loyalty_offer_id, created_by) VALUES ($1,$2,$3,$4)",
    )
    .bind(promotion_id)
    .bind(customer_id)
    .bind(loyalty_offer_id)
    .bind(created_by)
    .execute(&mut *conn)
    .await?;
    Ok(true)
}

/// Outcome of a `remove_eligibility`: `None` = there was no row (idempotent
/// unassign); `Some(offer_id)` = removed, carrying the row's `loyalty_offer_id`
/// (itself possibly `None` for pre-v1.3.0 / degraded rows).
pub async fn remove_eligibility(
    conn: &mut PgConnection,
    promotion_id: &str,
    customer_id: &str,
) -> Result<Option<Option<String>>, ApiError> {
    let row = sqlx::query(
        "SELECT id, loyalty_offer_id FROM catalog.promotion_eligibility \
         WHERE promotion_id = $1 AND customer_id = $2",
    )
    .bind(promotion_id)
    .bind(customer_id)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(row) = row else {
        return Ok(None);
    };
    let id: i64 = row.try_get("id")?;
    let loyalty_offer_id: Option<String> = row.try_get("loyalty_offer_id")?;
    sqlx::query("DELETE FROM catalog.promotion_eligibility WHERE id = $1")
        .bind(id)
        .execute(&mut *conn)
        .await?;
    Ok(Some(loyalty_offer_id))
}
