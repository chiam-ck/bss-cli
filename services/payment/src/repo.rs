//! Repositories — dumb CRUD over the `payment` schema + sequence IDs.
//!
//! Port of `app.repositories.*` + the `payment.customer` cache reads used by
//! `PaymentService._lookup_customer_external_ref` and the Stripe adapter's
//! `ensure_customer`. Money (`amount`) is read as `amount::text` → `Decimal` so
//! the 2dp scale is preserved exactly (`"25.00"` stays `"25.00"`); on write it is
//! bound as text and `CAST($n AS numeric)` (the P3 catalog/com idiom).

use std::str::FromStr;

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use sqlx::postgres::PgRow;
use sqlx::{PgConnection, Row};

use crate::error::ApiError;

// ── Row structs ──────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct PaymentAttemptRow {
    pub id: String,
    pub customer_id: String,
    pub payment_method_id: String,
    pub amount: Decimal,
    pub currency: String,
    pub purpose: String,
    pub status: String,
    pub gateway_ref: Option<String>,
    pub decline_reason: Option<String>,
    pub attempted_at: DateTime<Utc>,
}

#[derive(Debug, Clone)]
pub struct PaymentMethodRow {
    pub id: String,
    pub customer_id: String,
    pub type_: String,
    pub token: String,
    pub token_provider: String,
    pub last4: String,
    pub brand: Option<String>,
    pub exp_month: i16,
    pub exp_year: i16,
    pub is_default: bool,
    pub status: String,
    pub created_at: DateTime<Utc>,
}

fn decimal_from_text(row: &PgRow, col: &str) -> Result<Decimal, ApiError> {
    let t: String = row.try_get(col)?;
    Decimal::from_str(&t).map_err(|e| ApiError::Internal(format!("bad numeric in {col}: {e}")))
}

fn attempt_from_row(row: &PgRow) -> Result<PaymentAttemptRow, ApiError> {
    Ok(PaymentAttemptRow {
        id: row.try_get("id")?,
        customer_id: row.try_get("customer_id")?,
        payment_method_id: row.try_get("payment_method_id")?,
        amount: decimal_from_text(row, "amount")?,
        currency: row.try_get("currency")?,
        purpose: row.try_get("purpose")?,
        status: row.try_get("status")?,
        gateway_ref: row.try_get("gateway_ref")?,
        decline_reason: row.try_get("decline_reason")?,
        attempted_at: row.try_get("attempted_at")?,
    })
}

fn method_from_row(row: &PgRow) -> Result<PaymentMethodRow, ApiError> {
    Ok(PaymentMethodRow {
        id: row.try_get("id")?,
        customer_id: row.try_get("customer_id")?,
        type_: row.try_get("type")?,
        token: row.try_get("token")?,
        token_provider: row.try_get("token_provider")?,
        last4: row.try_get("last4")?,
        brand: row.try_get("brand")?,
        exp_month: row.try_get("exp_month")?,
        exp_year: row.try_get("exp_year")?,
        is_default: row.try_get("is_default")?,
        status: row.try_get("status")?,
        created_at: row.try_get("created_at")?,
    })
}

const ATTEMPT_COLS: &str = "id, customer_id, payment_method_id, amount::text AS amount, currency, \
     purpose, status, gateway_ref, decline_reason, attempted_at";
const METHOD_COLS: &str = "id, customer_id, type, token, token_provider, last4, brand, exp_month, \
     exp_year, is_default, status, created_at";

// ── PaymentAttempt ───────────────────────────────────────────────────

pub async fn next_attempt_id(conn: &mut PgConnection) -> Result<String, ApiError> {
    let seq: i64 = sqlx::query("SELECT nextval('payment.payment_attempt_id_seq')")
        .fetch_one(&mut *conn)
        .await?
        .try_get(0)?;
    Ok(format!("PAY-{seq:06}"))
}

#[allow(clippy::too_many_arguments)]
pub async fn insert_attempt(
    conn: &mut PgConnection,
    id: &str,
    customer_id: &str,
    payment_method_id: &str,
    amount: &Decimal,
    currency: &str,
    purpose: &str,
    status: &str,
    gateway_ref: &str,
    decline_reason: Option<&str>,
    provider_call_id: &str,
    decline_code: Option<&str>,
    idempotency_key: &str,
    attempted_at: DateTime<Utc>,
    tenant_id: &str,
) -> Result<(), ApiError> {
    sqlx::query(
        "INSERT INTO payment.payment_attempt \
         (id, customer_id, payment_method_id, amount, currency, purpose, status, gateway_ref, \
          decline_reason, provider_call_id, decline_code, idempotency_key, attempted_at, tenant_id) \
         VALUES ($1,$2,$3,CAST($4 AS numeric),$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
    )
    .bind(id)
    .bind(customer_id)
    .bind(payment_method_id)
    .bind(amount.to_string())
    .bind(currency)
    .bind(purpose)
    .bind(status)
    .bind(gateway_ref)
    .bind(decline_reason)
    .bind(provider_call_id)
    .bind(decline_code)
    .bind(idempotency_key)
    .bind(attempted_at)
    .bind(tenant_id)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

pub async fn get_attempt(
    pool: &sqlx::PgPool,
    id: &str,
) -> Result<Option<PaymentAttemptRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {ATTEMPT_COLS} FROM payment.payment_attempt WHERE id = $1"
    ))
    .bind(id)
    .fetch_optional(pool)
    .await?;
    row.as_ref().map(attempt_from_row).transpose()
}

pub async fn list_attempts(
    pool: &sqlx::PgPool,
    customer_id: &str,
    limit: Option<i64>,
    offset: Option<i64>,
) -> Result<Vec<PaymentAttemptRow>, ApiError> {
    // Match the oracle: ORDER BY attempted_at DESC, optional LIMIT/OFFSET.
    let mut sql = format!(
        "SELECT {ATTEMPT_COLS} FROM payment.payment_attempt WHERE customer_id = $1 \
         ORDER BY attempted_at DESC"
    );
    if limit.is_some() {
        sql.push_str(" LIMIT $2");
    }
    if offset.is_some() {
        sql.push_str(if limit.is_some() {
            " OFFSET $3"
        } else {
            " OFFSET $2"
        });
    }
    let mut q = sqlx::query(&sql).bind(customer_id);
    if let Some(l) = limit {
        q = q.bind(l);
    }
    if let Some(o) = offset {
        q = q.bind(o);
    }
    let rows = q.fetch_all(pool).await?;
    rows.iter().map(attempt_from_row).collect()
}

pub async fn count_attempts(pool: &sqlx::PgPool, customer_id: &str) -> Result<i64, ApiError> {
    let n: i64 = sqlx::query("SELECT count(*) FROM payment.payment_attempt WHERE customer_id = $1")
        .bind(customer_id)
        .fetch_one(pool)
        .await?
        .try_get(0)?;
    Ok(n)
}

/// Webhook reconciliation lookup — the attempt row for a Stripe `pi_*`.
/// Returns `(id, status)`.
pub async fn get_attempt_by_provider_call_id(
    conn: &mut PgConnection,
    provider_call_id: &str,
) -> Result<Option<(String, String)>, ApiError> {
    let row =
        sqlx::query("SELECT id, status FROM payment.payment_attempt WHERE provider_call_id = $1")
            .bind(provider_call_id)
            .fetch_optional(&mut *conn)
            .await?;
    match row {
        Some(r) => Ok(Some((r.try_get("id")?, r.try_get("status")?))),
        None => Ok(None),
    }
}

// ── PaymentMethod ────────────────────────────────────────────────────

pub async fn next_method_id(conn: &mut PgConnection) -> Result<String, ApiError> {
    let seq: i64 = sqlx::query("SELECT nextval('payment.payment_method_id_seq')")
        .fetch_one(&mut *conn)
        .await?
        .try_get(0)?;
    Ok(format!("PM-{seq:04}"))
}

pub async fn get_method(
    pool: &sqlx::PgPool,
    id: &str,
) -> Result<Option<PaymentMethodRow>, ApiError> {
    let row = sqlx::query(&format!(
        "SELECT {METHOD_COLS} FROM payment.payment_method WHERE id = $1"
    ))
    .bind(id)
    .fetch_optional(pool)
    .await?;
    row.as_ref().map(method_from_row).transpose()
}

pub async fn list_methods(
    pool: &sqlx::PgPool,
    customer_id: &str,
    include_removed: bool,
) -> Result<Vec<PaymentMethodRow>, ApiError> {
    let sql = if include_removed {
        format!(
            "SELECT {METHOD_COLS} FROM payment.payment_method WHERE customer_id = $1 \
             ORDER BY created_at DESC"
        )
    } else {
        format!(
            "SELECT {METHOD_COLS} FROM payment.payment_method WHERE customer_id = $1 \
             AND status = 'active' ORDER BY created_at DESC"
        )
    };
    let rows = sqlx::query(&sql).bind(customer_id).fetch_all(pool).await?;
    rows.iter().map(method_from_row).collect()
}

pub async fn count_active_methods(
    conn: &mut PgConnection,
    customer_id: &str,
) -> Result<i64, ApiError> {
    let n: i64 = sqlx::query(
        "SELECT count(*) FROM payment.payment_method WHERE customer_id = $1 AND status = 'active'",
    )
    .bind(customer_id)
    .fetch_one(&mut *conn)
    .await?
    .try_get(0)?;
    Ok(n)
}

#[allow(clippy::too_many_arguments)]
pub async fn insert_method(
    conn: &mut PgConnection,
    id: &str,
    customer_id: &str,
    type_: &str,
    token: &str,
    token_provider: &str,
    last4: &str,
    brand: &str,
    exp_month: i16,
    exp_year: i16,
    is_default: bool,
    tenant_id: &str,
) -> Result<(), ApiError> {
    sqlx::query(
        "INSERT INTO payment.payment_method \
         (id, customer_id, type, token, token_provider, last4, brand, exp_month, exp_year, \
          is_default, status, tenant_id) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'active',$11)",
    )
    .bind(id)
    .bind(customer_id)
    .bind(type_)
    .bind(token)
    .bind(token_provider)
    .bind(last4)
    .bind(brand)
    .bind(exp_month)
    .bind(exp_year)
    .bind(is_default)
    .bind(tenant_id)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

pub async fn set_method_status(
    conn: &mut PgConnection,
    id: &str,
    status: &str,
) -> Result<(), ApiError> {
    sqlx::query("UPDATE payment.payment_method SET status = $2, updated_at = now() WHERE id = $1")
        .bind(id)
        .bind(status)
        .execute(&mut *conn)
        .await?;
    Ok(())
}

/// Clear the existing default for `customer_id`, then set `pm_id` as default —
/// both in the caller's transaction. Port of `PaymentMethodRepository.set_default`.
pub async fn set_default(
    conn: &mut PgConnection,
    customer_id: &str,
    pm_id: &str,
) -> Result<(), ApiError> {
    sqlx::query(
        "UPDATE payment.payment_method SET is_default = false \
         WHERE customer_id = $1 AND id <> $2 AND is_default = true",
    )
    .bind(customer_id)
    .bind(pm_id)
    .execute(&mut *conn)
    .await?;
    sqlx::query("UPDATE payment.payment_method SET is_default = true WHERE id = $1")
        .bind(pm_id)
        .execute(&mut *conn)
        .await?;
    Ok(())
}

/// Active mock-token rows for the v0.16 cutover invalidation. Returns the rows'
/// `(id, customer_id, last4, brand, token_provider)`.
pub async fn list_active_mock_methods(
    conn: &mut PgConnection,
) -> Result<Vec<(String, String, String, Option<String>, String)>, ApiError> {
    let rows = sqlx::query(
        "SELECT id, customer_id, last4, brand, token_provider FROM payment.payment_method \
         WHERE token_provider = 'mock' AND status = 'active'",
    )
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|r| {
            Ok((
                r.try_get("id")?,
                r.try_get("customer_id")?,
                r.try_get("last4")?,
                r.try_get("brand")?,
                r.try_get("token_provider")?,
            ))
        })
        .collect()
}

// ── payment.customer cache (provider-side customer ref) ──────────────

/// Read the cached provider-side customer ref (`cus_*`) for a BSS customer,
/// provider-agnostic. Port of `PaymentService._lookup_customer_external_ref`.
pub async fn lookup_customer_external_ref(
    pool: &sqlx::PgPool,
    customer_id: &str,
) -> Result<Option<String>, ApiError> {
    let row = sqlx::query("SELECT customer_external_ref FROM payment.customer WHERE id = $1")
        .bind(customer_id)
        .fetch_optional(pool)
        .await?;
    match row {
        Some(r) => Ok(r.try_get("customer_external_ref")?),
        None => Ok(None),
    }
}

/// Read the cached ref scoped to a provider — the Stripe `ensure_customer` cache
/// check (`WHERE id = $1 AND customer_external_ref_provider = $2`).
pub async fn lookup_customer_external_ref_for_provider(
    pool: &sqlx::PgPool,
    customer_id: &str,
    provider: &str,
) -> Result<Option<String>, ApiError> {
    let row = sqlx::query(
        "SELECT customer_external_ref FROM payment.customer \
         WHERE id = $1 AND customer_external_ref_provider = $2",
    )
    .bind(customer_id)
    .bind(provider)
    .fetch_optional(pool)
    .await?;
    match row {
        Some(r) => Ok(r.try_get("customer_external_ref")?),
        None => Ok(None),
    }
}

/// Persist a `payment.customer` cache row (Stripe `ensure_customer`). `tenant_id`
/// is left to its `'DEFAULT'` column default — the oracle's `PaymentCustomer(...)`
/// doesn't set it either.
pub async fn insert_payment_customer(
    pool: &sqlx::PgPool,
    customer_id: &str,
    external_ref: &str,
    provider: &str,
) -> Result<(), ApiError> {
    sqlx::query(
        "INSERT INTO payment.customer \
         (id, customer_external_ref, customer_external_ref_provider) \
         VALUES ($1,$2,$3)",
    )
    .bind(customer_id)
    .bind(external_ref)
    .bind(provider)
    .execute(pool)
    .await?;
    Ok(())
}
