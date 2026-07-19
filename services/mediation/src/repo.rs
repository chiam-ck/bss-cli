//! `mediation.usage_event` persistence — port of `app.repositories.usage_repo`.
//!
//! Raw sqlx (no ORM). `next_id` draws from the same `usage_event_id_seq` the
//! Python repo uses, so ids stay `UE-000042`-shaped and monotonic across the
//! language boundary during cutover. The `UsageEventRow` is the per-table struct
//! that lands with this service (doctrine: model structs port with their service).

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sqlx::postgres::{PgConnection, PgRow};
use sqlx::Row;

use crate::domain::UsageEvent;

/// The TMF635 usage path — the `href` prefix and the router mount point.
pub const USAGE_PATH: &str = "/tmf-api/usageManagement/v4/usage";

/// A row of `mediation.usage_event`, read back for the GET/list endpoints.
/// Mapped manually (the workspace sqlx has `default-features = false`, so the
/// `FromRow` derive isn't compiled — the codebase reads columns via `row.get`).
#[derive(Debug, Clone)]
pub struct UsageEventRow {
    pub id: String,
    pub msisdn: String,
    pub subscription_id: Option<String>,
    pub event_type: String,
    pub event_time: DateTime<Utc>,
    pub quantity: i64,
    pub unit: String,
    pub source: Option<String>,
    pub raw_cdr_ref: Option<String>,
    pub processed: bool,
    pub processing_error: Option<String>,
    pub roaming_indicator: bool,
}

impl UsageEventRow {
    fn from_row(r: &PgRow) -> Self {
        UsageEventRow {
            id: r.get("id"),
            msisdn: r.get("msisdn"),
            subscription_id: r.get("subscription_id"),
            event_type: r.get("event_type"),
            event_time: r.get("event_time"),
            quantity: r.get("quantity"),
            unit: r.get("unit"),
            source: r.get("source"),
            raw_cdr_ref: r.get("raw_cdr_ref"),
            processed: r.get("processed"),
            processing_error: r.get("processing_error"),
            roaming_indicator: r.get("roaming_indicator"),
        }
    }
}

/// `SELECT nextval('mediation.usage_event_id_seq')` → `UE-000042`. Runs on the
/// same connection/transaction as the insert.
pub async fn next_id(conn: &mut PgConnection) -> Result<String, sqlx::Error> {
    let seq: i64 = sqlx::query_scalar("SELECT nextval('mediation.usage_event_id_seq')")
        .fetch_one(conn)
        .await?;
    Ok(format!("UE-{seq:06}"))
}

/// Insert the `usage_event` row (only after every policy has passed — no row
/// exists for a rejected event). `created_at`/`updated_at` are filled by the
/// column defaults; `processed` starts false.
pub async fn insert(
    conn: &mut PgConnection,
    evt: &UsageEvent,
    tenant_id: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO mediation.usage_event \
         (id, msisdn, subscription_id, event_type, event_time, quantity, unit, source, \
          raw_cdr_ref, processed, roaming_indicator, tenant_id) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,false,$10,$11)",
    )
    .bind(&evt.id)
    .bind(&evt.msisdn)
    .bind(&evt.subscription_id)
    .bind(&evt.event_type)
    .bind(evt.event_time)
    .bind(evt.quantity)
    .bind(&evt.unit)
    .bind(&evt.source)
    .bind(&evt.raw_cdr_ref)
    .bind(evt.roaming_indicator)
    .bind(tenant_id)
    .execute(conn)
    .await?;
    Ok(())
}

/// `GET /usage/{id}` support.
pub async fn get(
    pool: &sqlx::PgPool,
    event_id: &str,
) -> Result<Option<UsageEventRow>, sqlx::Error> {
    let row = sqlx::query("SELECT * FROM mediation.usage_event WHERE id = $1")
        .bind(event_id)
        .fetch_optional(pool)
        .await?;
    Ok(row.map(|r| UsageEventRow::from_row(&r)))
}

/// Filters for the list endpoint — all optional, `AND`-combined, newest first.
#[derive(Debug, Default)]
pub struct ListFilters {
    pub subscription_id: Option<String>,
    pub msisdn: Option<String>,
    pub event_type: Option<String>,
    pub since: Option<DateTime<Utc>>,
    pub limit: i64,
}

/// `GET /usage` support — dynamic filters, `event_time DESC`, capped by `limit`.
pub async fn list_by_filters(
    pool: &sqlx::PgPool,
    f: &ListFilters,
) -> Result<Vec<UsageEventRow>, sqlx::Error> {
    let mut qb = sqlx::QueryBuilder::new("SELECT * FROM mediation.usage_event WHERE 1=1");
    if let Some(sid) = &f.subscription_id {
        qb.push(" AND subscription_id = ").push_bind(sid);
    }
    if let Some(m) = &f.msisdn {
        qb.push(" AND msisdn = ").push_bind(m);
    }
    if let Some(t) = &f.event_type {
        qb.push(" AND event_type = ").push_bind(t);
    }
    if let Some(s) = &f.since {
        qb.push(" AND event_time >= ").push_bind(s);
    }
    qb.push(" ORDER BY event_time DESC LIMIT ")
        .push_bind(f.limit);
    let rows = qb.build().fetch_all(pool).await?;
    Ok(rows.iter().map(UsageEventRow::from_row).collect())
}

/// The TMF635 `UsageResponse` body — port of `to_usage_response`.
pub fn to_response(row: &UsageEventRow) -> Value {
    json!({
        "id": row.id,
        "href": format!("{USAGE_PATH}/{}", row.id),
        "msisdn": row.msisdn,
        "subscriptionId": row.subscription_id,
        "eventType": row.event_type,
        "eventTime": bss_clock::isoformat(row.event_time),
        "quantity": row.quantity,
        "unit": row.unit,
        "source": row.source,
        "rawCdrRef": row.raw_cdr_ref,
        "processed": row.processed,
        "processingError": row.processing_error,
        "roamingIndicator": row.roaming_indicator,
        "@type": "Usage",
    })
}
