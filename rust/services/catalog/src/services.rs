//! Catalog admin write paths — port of `CatalogAdminService`.
//!
//! Reads stay on `repo`; writes go through here so the admin gate + structural
//! validation are consistent with every other service. Money is bound as text and
//! `CAST(... AS numeric)` (no sqlx decimal feature needed). Each verb commits in
//! one transaction, then re-reads the aggregate for the response (as the oracle
//! does). `tenant_id`/timestamps rely on their column server-defaults.

use bss_db::{PgPool, PolicyViolation};
use rust_decimal::Decimal;
use serde_json::json;

use crate::error::ApiError;
use crate::repo::{self, OfferingFull, PriceRow};

/// v0.7 admin gate: any non-empty, non-anonymous actor is admin. Empty /
/// "anonymous" is rejected — the actor arrives via `X-BSS-Actor`.
pub fn check_admin(actor: &str) -> Result<(), ApiError> {
    if actor.is_empty() || actor == "anonymous" {
        return Err(PolicyViolation::with_context(
            "catalog.admin_only",
            "Catalog write operations require an authenticated admin actor",
            json!({ "actor": actor }),
        )
        .into());
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub struct AddOffering<'a> {
    pub offering_id: &'a str,
    pub name: &'a str,
    pub spec_id: &'a str,
    pub amount: Decimal,
    pub currency: &'a str,
    pub price_id: Option<&'a str>,
    pub valid_from: Option<chrono::DateTime<chrono::Utc>>,
    pub valid_to: Option<chrono::DateTime<chrono::Utc>>,
    pub data_mb: Option<i64>,
    pub voice_minutes: Option<i64>,
    pub sms_count: Option<i64>,
    pub data_roaming_mb: Option<i64>,
}

pub async fn add_offering(
    pool: &PgPool,
    actor: &str,
    req: AddOffering<'_>,
) -> Result<OfferingFull, ApiError> {
    check_admin(actor)?;

    if repo::get_offering(pool, req.offering_id).await?.is_some() {
        return Err(PolicyViolation::with_context(
            "catalog.offering.already_exists",
            format!("Offering {} already exists", req.offering_id),
            json!({ "offering_id": req.offering_id }),
        )
        .into());
    }

    let resolved_price_id = req
        .price_id
        .map(str::to_string)
        .unwrap_or_else(|| format!("PRICE_{}", req.offering_id));

    let mut tx = pool.begin().await?;

    sqlx::query(
        "INSERT INTO catalog.product_offering \
         (id, name, spec_id, is_bundle, is_sellable, lifecycle_status, valid_from, valid_to) \
         VALUES ($1,$2,$3,true,true,'active',$4,$5)",
    )
    .bind(req.offering_id)
    .bind(req.name)
    .bind(req.spec_id)
    .bind(req.valid_from)
    .bind(req.valid_to)
    .execute(&mut *tx)
    .await?;

    sqlx::query(
        "INSERT INTO catalog.product_offering_price \
         (id, offering_id, price_type, recurring_period_length, recurring_period_type, \
          amount, currency, valid_from, valid_to) \
         VALUES ($1,$2,'recurring',1,'month',CAST($3 AS numeric),$4,$5,$6)",
    )
    .bind(&resolved_price_id)
    .bind(req.offering_id)
    .bind(req.amount.to_string())
    .bind(req.currency)
    .bind(req.valid_from)
    .bind(req.valid_to)
    .execute(&mut *tx)
    .await?;

    // Allowances in the oracle's insertion order (data, voice, sms, data_roaming).
    for (val, kind, unit, suffix) in [
        (req.data_mb, "data", "mb", "DATA"),
        (req.voice_minutes, "voice", "minutes", "VOICE"),
        (req.sms_count, "sms", "count", "SMS"),
        (req.data_roaming_mb, "data_roaming", "mb", "ROAM"),
    ] {
        if let Some(qty) = val {
            sqlx::query(
                "INSERT INTO catalog.bundle_allowance (id, offering_id, allowance_type, quantity, unit) \
                 VALUES ($1,$2,$3,$4,$5)",
            )
            .bind(format!("BA_{}_{}", req.offering_id, suffix))
            .bind(req.offering_id)
            .bind(kind)
            .bind(qty)
            .bind(unit)
            .execute(&mut *tx)
            .await?;
        }
    }

    tx.commit().await?;
    tracing::info!(offering_id = req.offering_id, actor, amount = %req.amount, "catalog.offering.added");

    repo::get_offering(pool, req.offering_id)
        .await?
        .ok_or_else(|| ApiError::Internal("offering vanished after insert".into()))
}

pub async fn set_offering_window(
    pool: &PgPool,
    actor: &str,
    offering_id: &str,
    valid_from: Option<chrono::DateTime<chrono::Utc>>,
    valid_to: Option<chrono::DateTime<chrono::Utc>>,
) -> Result<OfferingFull, ApiError> {
    check_admin(actor)?;

    let mut tx = pool.begin().await?;
    if repo::get_offering_row(&mut tx, offering_id)
        .await?
        .is_none()
    {
        return Err(not_found(offering_id));
    }

    // Auto-retire when the window closes at-or-before now.
    let now = bss_clock::now();
    let auto_retire = valid_to.map(|vt| vt <= now).unwrap_or(false);
    if auto_retire {
        sqlx::query(
            "UPDATE catalog.product_offering \
             SET valid_from = $2, valid_to = $3, lifecycle_status = 'retired', is_sellable = false, \
                 updated_at = now() WHERE id = $1",
        )
        .bind(offering_id)
        .bind(valid_from)
        .bind(valid_to)
        .execute(&mut *tx)
        .await?;
    } else {
        sqlx::query(
            "UPDATE catalog.product_offering \
             SET valid_from = $2, valid_to = $3, updated_at = now() WHERE id = $1",
        )
        .bind(offering_id)
        .bind(valid_from)
        .bind(valid_to)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    tracing::info!(
        offering_id,
        actor,
        auto_retired = auto_retire,
        "catalog.offering.windowed"
    );

    repo::get_offering(pool, offering_id)
        .await?
        .ok_or_else(|| ApiError::Internal("offering vanished after update".into()))
}

pub async fn retire_offering(
    pool: &PgPool,
    actor: &str,
    offering_id: &str,
) -> Result<OfferingFull, ApiError> {
    check_admin(actor)?;

    let mut tx = pool.begin().await?;
    let existing = match repo::get_offering_row(&mut tx, offering_id).await? {
        Some(o) => o,
        None => return Err(not_found(offering_id)),
    };
    if existing.lifecycle_status.as_deref() == Some("retired") {
        return Err(PolicyViolation::with_context(
            "catalog.offering.already_retired",
            format!("Offering {offering_id} is already retired"),
            json!({ "offering_id": offering_id }),
        )
        .into());
    }

    let now = bss_clock::now();
    let stamp_valid_to = existing.valid_to.map(|vt| vt > now).unwrap_or(true);
    if stamp_valid_to {
        sqlx::query(
            "UPDATE catalog.product_offering \
             SET lifecycle_status = 'retired', is_sellable = false, valid_to = $2, updated_at = now() \
             WHERE id = $1",
        )
        .bind(offering_id)
        .bind(now)
        .execute(&mut *tx)
        .await?;
    } else {
        sqlx::query(
            "UPDATE catalog.product_offering \
             SET lifecycle_status = 'retired', is_sellable = false, updated_at = now() WHERE id = $1",
        )
        .bind(offering_id)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    tracing::info!(offering_id, actor, "catalog.offering.retired");

    repo::get_offering(pool, offering_id)
        .await?
        .ok_or_else(|| ApiError::Internal("offering vanished after retire".into()))
}

#[allow(clippy::too_many_arguments)]
pub async fn add_price(
    pool: &PgPool,
    actor: &str,
    offering_id: &str,
    price_id: &str,
    amount: Decimal,
    currency: &str,
    valid_from: Option<chrono::DateTime<chrono::Utc>>,
    valid_to: Option<chrono::DateTime<chrono::Utc>>,
    retire_current: bool,
) -> Result<PriceRow, ApiError> {
    check_admin(actor)?;

    if repo::get_offering(pool, offering_id).await?.is_none() {
        return Err(not_found(offering_id));
    }
    if repo::get_price_by_id(pool, price_id).await?.is_some() {
        return Err(PolicyViolation::with_context(
            "catalog.price.already_exists",
            format!("Price {price_id} already exists"),
            json!({ "price_id": price_id }),
        )
        .into());
    }

    let mut tx = pool.begin().await?;
    if retire_current {
        let cut = valid_from.unwrap_or_else(bss_clock::now);
        sqlx::query(
            "UPDATE catalog.product_offering_price SET valid_to = $2, updated_at = now() \
             WHERE offering_id = $1 AND valid_to IS NULL",
        )
        .bind(offering_id)
        .bind(cut)
        .execute(&mut *tx)
        .await?;
    }
    sqlx::query(
        "INSERT INTO catalog.product_offering_price \
         (id, offering_id, price_type, recurring_period_length, recurring_period_type, \
          amount, currency, valid_from, valid_to) \
         VALUES ($1,$2,'recurring',1,'month',CAST($3 AS numeric),$4,$5,$6)",
    )
    .bind(price_id)
    .bind(offering_id)
    .bind(amount.to_string())
    .bind(currency)
    .bind(valid_from)
    .bind(valid_to)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    tracing::info!(
        offering_id,
        price_id,
        actor,
        retire_current,
        "catalog.price.added"
    );

    repo::get_price_by_id(pool, price_id)
        .await?
        .ok_or_else(|| ApiError::Internal("price vanished after insert".into()))
}

fn not_found(offering_id: &str) -> ApiError {
    PolicyViolation::with_context(
        "catalog.offering.not_found",
        format!("Offering {offering_id} not found"),
        json!({ "offering_id": offering_id }),
    )
    .into()
}
