//! `catalog` schema reads — port of `CatalogRepository`.
//!
//! Dumb reads over the ORM tables. Money columns are read as `amount::text` and
//! parsed into `rust_decimal::Decimal` so the 2dp scale is preserved exactly
//! (the wire renders `taxIncludedAmount.value` as a float but the promo math and
//! `discountValue` string need the exact decimal). Prices and allowances carry
//! **no ORDER BY** — the oracle's `selectinload` doesn't either, so both read the
//! same physical (insertion) order from the same Postgres.

use std::str::FromStr;

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use sqlx::postgres::PgRow;
use sqlx::{PgConnection, PgPool, Row};

use crate::error::ApiError;

// ── row structs (mirror bss_models.catalog) ──────────────────────────────────

#[derive(Debug, Clone)]
pub struct OfferingRow {
    pub id: String,
    pub name: Option<String>,
    pub is_bundle: bool,
    pub is_sellable: Option<bool>,
    pub lifecycle_status: Option<String>,
    pub valid_from: Option<DateTime<Utc>>,
    pub valid_to: Option<DateTime<Utc>>,
    pub spec_id: Option<String>,
}

#[derive(Debug, Clone)]
pub struct PriceRow {
    pub id: String,
    pub price_type: String,
    pub recurring_period_length: Option<i16>,
    pub recurring_period_type: Option<String>,
    pub amount: Decimal,
    pub currency: String,
    pub valid_from: Option<DateTime<Utc>>,
    pub valid_to: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone)]
pub struct AllowanceRow {
    pub allowance_type: String,
    pub quantity: i64,
    pub unit: String,
}

#[derive(Debug, Clone)]
pub struct SpecRow {
    pub id: String,
    pub name: Option<String>,
    pub description: Option<String>,
    pub brand: Option<String>,
    pub lifecycle_status: Option<String>,
}

#[derive(Debug, Clone)]
pub struct VasRow {
    pub id: String,
    pub name: Option<String>,
    pub price_amount: Decimal,
    pub currency: String,
    pub allowance_type: Option<String>,
    pub allowance_quantity: Option<i64>,
    pub allowance_unit: Option<String>,
    pub expiry_hours: Option<i16>,
}

/// An offering with its eager-loaded spec, prices, and allowances (the
/// `selectinload` bundle the TMF mapping needs).
#[derive(Debug, Clone)]
pub struct OfferingFull {
    pub offering: OfferingRow,
    pub spec: Option<SpecRow>,
    pub prices: Vec<PriceRow>,
    pub allowances: Vec<AllowanceRow>,
}

// ── column mappers ────────────────────────────────────────────────────────────

fn decimal_col(row: &PgRow, col: &str) -> Result<Decimal, ApiError> {
    let text: String = row.try_get(col).map_err(ApiError::from)?;
    Decimal::from_str(&text).map_err(|e| ApiError::Internal(format!("bad decimal in {col}: {e}")))
}

fn offering_from_row(row: &PgRow) -> Result<OfferingRow, ApiError> {
    Ok(OfferingRow {
        id: row.try_get("id")?,
        name: row.try_get("name")?,
        is_bundle: row.try_get("is_bundle")?,
        is_sellable: row.try_get("is_sellable")?,
        lifecycle_status: row.try_get("lifecycle_status")?,
        valid_from: row.try_get("valid_from")?,
        valid_to: row.try_get("valid_to")?,
        spec_id: row.try_get("spec_id")?,
    })
}

fn price_from_row(row: &PgRow) -> Result<PriceRow, ApiError> {
    Ok(PriceRow {
        id: row.try_get("id")?,
        price_type: row.try_get("price_type")?,
        recurring_period_length: row.try_get("recurring_period_length")?,
        recurring_period_type: row.try_get("recurring_period_type")?,
        amount: decimal_col(row, "amount")?,
        currency: row.try_get("currency")?,
        valid_from: row.try_get("valid_from")?,
        valid_to: row.try_get("valid_to")?,
    })
}

const OFFERING_COLS: &str =
    "id, name, spec_id, is_bundle, is_sellable, lifecycle_status, valid_from, valid_to";
const PRICE_COLS: &str = "id, price_type, recurring_period_length, recurring_period_type, \
     amount::text AS amount, currency, valid_from, valid_to";

// ── offering reads ────────────────────────────────────────────────────────────

async fn load_full(pool: &PgPool, offering: OfferingRow) -> Result<OfferingFull, ApiError> {
    let spec = match &offering.spec_id {
        Some(sid) => get_spec(pool, sid).await?,
        None => None,
    };
    let price_rows = sqlx::query(&format!(
        "SELECT {PRICE_COLS} FROM catalog.product_offering_price WHERE offering_id = $1"
    ))
    .bind(&offering.id)
    .fetch_all(pool)
    .await?;
    let mut prices = Vec::with_capacity(price_rows.len());
    for r in &price_rows {
        prices.push(price_from_row(r)?);
    }
    let allow_rows = sqlx::query(
        "SELECT allowance_type, quantity, unit FROM catalog.bundle_allowance WHERE offering_id = $1",
    )
    .bind(&offering.id)
    .fetch_all(pool)
    .await?;
    let allowances = allow_rows
        .iter()
        .map(|r| {
            Ok(AllowanceRow {
                allowance_type: r.try_get("allowance_type")?,
                quantity: r.try_get("quantity")?,
                unit: r.try_get("unit")?,
            })
        })
        .collect::<Result<Vec<_>, ApiError>>()?;
    Ok(OfferingFull {
        offering,
        spec,
        prices,
        allowances,
    })
}

pub async fn list_offerings(
    pool: &PgPool,
    lifecycle_status: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<OfferingFull>, ApiError> {
    let base = format!("SELECT {OFFERING_COLS} FROM catalog.product_offering");
    let rows = match lifecycle_status {
        Some(status) => {
            sqlx::query(&format!(
                "{base} WHERE lifecycle_status = $1 ORDER BY id LIMIT $2 OFFSET $3"
            ))
            .bind(status)
            .bind(limit)
            .bind(offset)
            .fetch_all(pool)
            .await?
        }
        None => {
            sqlx::query(&format!("{base} ORDER BY id LIMIT $1 OFFSET $2"))
                .bind(limit)
                .bind(offset)
                .fetch_all(pool)
                .await?
        }
    };
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        out.push(load_full(pool, offering_from_row(r)?).await?);
    }
    Ok(out)
}

pub async fn get_offering(
    pool: &PgPool,
    offering_id: &str,
) -> Result<Option<OfferingFull>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {OFFERING_COLS} FROM catalog.product_offering WHERE id = $1"
    ))
    .bind(offering_id)
    .fetch_optional(pool)
    .await?;
    match row {
        Some(r) => Ok(Some(load_full(pool, offering_from_row(&r)?).await?)),
        None => Ok(None),
    }
}

/// Offerings sellable at `moment` — time-bound + is_sellable + lifecycle=active,
/// then re-ordered by lowest active recurring price (offerings with no active
/// price float to the end). Port of `list_active_offerings`.
pub async fn list_active_offerings(
    pool: &PgPool,
    moment: DateTime<Utc>,
    limit: i64,
    offset: i64,
) -> Result<Vec<OfferingFull>, ApiError> {
    let rows = sqlx::query(&format!(
        "SELECT {OFFERING_COLS} FROM catalog.product_offering \
         WHERE is_sellable IS TRUE AND lifecycle_status = 'active' \
           AND (valid_from IS NULL OR valid_from <= $1) \
           AND (valid_to IS NULL OR valid_to > $1) \
         ORDER BY id LIMIT $2 OFFSET $3"
    ))
    .bind(moment)
    .bind(limit)
    .bind(offset)
    .fetch_all(pool)
    .await?;

    // (sort_bucket, price, id) — bucket 0 = has active price, 1 = none.
    let mut keyed: Vec<((i32, Decimal, String), OfferingFull)> = Vec::with_capacity(rows.len());
    for r in &rows {
        let full = load_full(pool, offering_from_row(r)?).await?;
        let key = match active_price(pool, &full.offering.id, moment).await? {
            Some(p) => (0, p.amount, full.offering.id.clone()),
            None => (1, Decimal::ZERO, full.offering.id.clone()),
        };
        keyed.push((key, full));
    }
    keyed.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(keyed.into_iter().map(|(_, f)| f).collect())
}

/// Lowest-amount recurring price row active on `offering_id` at `moment`, or
/// `None` when none match. Port of `get_active_price` (the `PolicyViolation` is
/// raised by the caller/route so this stays a plain read).
pub async fn active_price(
    pool: &PgPool,
    offering_id: &str,
    moment: DateTime<Utc>,
) -> Result<Option<PriceRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {PRICE_COLS} FROM catalog.product_offering_price \
         WHERE offering_id = $1 AND price_type = 'recurring' \
           AND (valid_from IS NULL OR valid_from <= $2) \
           AND (valid_to IS NULL OR valid_to > $2) \
         ORDER BY amount ASC, id ASC LIMIT 1"
    ))
    .bind(offering_id)
    .bind(moment)
    .fetch_optional(pool)
    .await?;
    row.map(|r| price_from_row(&r)).transpose()
}

/// Direct price lookup by id — no time filter (renewal snapshot resolve).
pub async fn get_price_by_id(pool: &PgPool, price_id: &str) -> Result<Option<PriceRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {PRICE_COLS} FROM catalog.product_offering_price WHERE id = $1"
    ))
    .bind(price_id)
    .fetch_optional(pool)
    .await?;
    row.map(|r| price_from_row(&r)).transpose()
}

// ── specification reads ───────────────────────────────────────────────────────

fn spec_from_row(row: &PgRow) -> Result<SpecRow, ApiError> {
    Ok(SpecRow {
        id: row.try_get("id")?,
        name: row.try_get("name")?,
        description: row.try_get("description")?,
        brand: row.try_get("brand")?,
        lifecycle_status: row.try_get("lifecycle_status")?,
    })
}

pub async fn list_specifications(
    pool: &PgPool,
    limit: i64,
    offset: i64,
) -> Result<Vec<SpecRow>, ApiError> {
    let rows = sqlx::query(
        "SELECT id, name, description, brand, lifecycle_status FROM catalog.product_specification \
         ORDER BY id LIMIT $1 OFFSET $2",
    )
    .bind(limit)
    .bind(offset)
    .fetch_all(pool)
    .await?;
    rows.iter().map(spec_from_row).collect()
}

pub async fn get_spec(pool: &PgPool, spec_id: &str) -> Result<Option<SpecRow>, ApiError> {
    let row = sqlx::query(
        "SELECT id, name, description, brand, lifecycle_status FROM catalog.product_specification WHERE id = $1",
    )
    .bind(spec_id)
    .fetch_optional(pool)
    .await?;
    row.as_ref().map(spec_from_row).transpose()
}

// ── VAS reads ─────────────────────────────────────────────────────────────────

fn vas_from_row(row: &PgRow) -> Result<VasRow, ApiError> {
    Ok(VasRow {
        id: row.try_get("id")?,
        name: row.try_get("name")?,
        price_amount: decimal_col(row, "price_amount")?,
        currency: row.try_get("currency")?,
        allowance_type: row.try_get("allowance_type")?,
        allowance_quantity: row.try_get("allowance_quantity")?,
        allowance_unit: row.try_get("allowance_unit")?,
        expiry_hours: row.try_get("expiry_hours")?,
    })
}

const VAS_COLS: &str = "id, name, price_amount::text AS price_amount, currency, allowance_type, \
     allowance_quantity, allowance_unit, expiry_hours";

pub async fn list_vas(pool: &PgPool, limit: i64, offset: i64) -> Result<Vec<VasRow>, ApiError> {
    let rows = sqlx::query(&format!(
        "SELECT {VAS_COLS} FROM catalog.vas_offering ORDER BY id LIMIT $1 OFFSET $2"
    ))
    .bind(limit)
    .bind(offset)
    .fetch_all(pool)
    .await?;
    rows.iter().map(vas_from_row).collect()
}

pub async fn get_vas(pool: &PgPool, vas_id: &str) -> Result<Option<VasRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {VAS_COLS} FROM catalog.vas_offering WHERE id = $1"
    ))
    .bind(vas_id)
    .fetch_optional(pool)
    .await?;
    row.as_ref().map(vas_from_row).transpose()
}

/// Read an offering row on an existing connection (admin write paths that need
/// existence/current-state checks inside their transaction).
pub async fn get_offering_row(
    conn: &mut PgConnection,
    offering_id: &str,
) -> Result<Option<OfferingRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {OFFERING_COLS} FROM catalog.product_offering WHERE id = $1"
    ))
    .bind(offering_id)
    .fetch_optional(conn)
    .await?;
    row.as_ref().map(offering_from_row).transpose()
}
