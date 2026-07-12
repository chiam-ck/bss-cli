//! `service_inventory` persistence — port of `service_order_repo` + `service_repo`.
//!
//! Mutations run on the consumer's `&mut PgConnection` (so the inbox claim + all
//! writes commit atomically); reads run on the pool. Targeted UPDATEs mirror the
//! specific ORM field-sets the Python service performs at each step.

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sqlx::postgres::{PgConnection, PgRow};
use sqlx::{PgPool, Row};

pub const SO_PATH: &str = "/tmf-api/serviceOrderingManagement/v4/serviceOrder";
pub const SVC_PATH: &str = "/tmf-api/serviceInventoryManagement/v4/service";

// ── ID sequences ────────────────────────────────────────────────────────────

pub async fn next_service_order_id(conn: &mut PgConnection) -> Result<String, sqlx::Error> {
    let n: i64 = sqlx::query_scalar("SELECT nextval('service_inventory.service_order_id_seq')")
        .fetch_one(conn)
        .await?;
    Ok(format!("SO-{n:04}"))
}

pub async fn next_service_order_item_id(conn: &mut PgConnection) -> Result<String, sqlx::Error> {
    let n: i64 =
        sqlx::query_scalar("SELECT nextval('service_inventory.service_order_item_id_seq')")
            .fetch_one(conn)
            .await?;
    Ok(format!("SOI-{n:04}"))
}

pub async fn next_service_id(conn: &mut PgConnection) -> Result<String, sqlx::Error> {
    let n: i64 = sqlx::query_scalar("SELECT nextval('service_inventory.service_id_seq')")
        .fetch_one(conn)
        .await?;
    Ok(format!("SVC-{n:04}"))
}

// ── ServiceOrder writes ───────────────────────────────────────────────────────

pub async fn insert_service_order(
    conn: &mut PgConnection,
    id: &str,
    commercial_order_id: &str,
    state: &str,
    tenant: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO service_inventory.service_order (id, commercial_order_id, state, tenant_id) \
         VALUES ($1,$2,$3,$4)",
    )
    .bind(id)
    .bind(commercial_order_id)
    .bind(state)
    .bind(tenant)
    .execute(conn)
    .await?;
    Ok(())
}

pub async fn insert_service_order_item(
    conn: &mut PgConnection,
    id: &str,
    service_order_id: &str,
    action: &str,
    service_spec_id: &str,
    target_service_id: Option<&str>,
    tenant: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO service_inventory.service_order_item \
         (id, service_order_id, action, service_spec_id, target_service_id, tenant_id) \
         VALUES ($1,$2,$3,$4,$5,$6)",
    )
    .bind(id)
    .bind(service_order_id)
    .bind(action)
    .bind(service_spec_id)
    .bind(target_service_id)
    .bind(tenant)
    .execute(conn)
    .await?;
    Ok(())
}

pub async fn set_soi_target(
    conn: &mut PgConnection,
    soi_id: &str,
    target_service_id: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE service_inventory.service_order_item SET target_service_id = $2, updated_at = now() \
         WHERE id = $1",
    )
    .bind(soi_id)
    .bind(target_service_id)
    .execute(conn)
    .await?;
    Ok(())
}

/// Transition a ServiceOrder, optionally stamping `started_at` / `completed_at`.
pub async fn set_service_order_state(
    conn: &mut PgConnection,
    id: &str,
    state: &str,
    started_at: Option<DateTime<Utc>>,
    completed_at: Option<DateTime<Utc>>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE service_inventory.service_order \
         SET state = $2, \
             started_at = COALESCE($3, started_at), \
             completed_at = COALESCE($4, completed_at), \
             updated_at = now() \
         WHERE id = $1",
    )
    .bind(id)
    .bind(state)
    .bind(started_at)
    .bind(completed_at)
    .execute(conn)
    .await?;
    Ok(())
}

/// `(state)` of a ServiceOrder — used to guard the transition before writing.
pub async fn service_order_state(
    conn: &mut PgConnection,
    id: &str,
) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar("SELECT state FROM service_inventory.service_order WHERE id = $1")
        .bind(id)
        .fetch_optional(conn)
        .await
}

// ── Service (CFS/RFS) writes ──────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
pub async fn insert_service(
    conn: &mut PgConnection,
    id: &str,
    spec_id: &str,
    type_: &str,
    parent_service_id: Option<&str>,
    state: &str,
    characteristics: &Value,
    tenant: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO service_inventory.service \
         (id, spec_id, type, parent_service_id, state, characteristics, tenant_id) \
         VALUES ($1,$2,$3,$4,$5,$6,$7)",
    )
    .bind(id)
    .bind(spec_id)
    .bind(type_)
    .bind(parent_service_id)
    .bind(state)
    .bind(sqlx::types::Json(characteristics))
    .bind(tenant)
    .execute(conn)
    .await?;
    Ok(())
}

pub async fn set_service_state(
    conn: &mut PgConnection,
    id: &str,
    state: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE service_inventory.service SET state = $2, updated_at = now() WHERE id = $1",
    )
    .bind(id)
    .bind(state)
    .execute(conn)
    .await?;
    Ok(())
}

pub async fn set_service_characteristics(
    conn: &mut PgConnection,
    id: &str,
    characteristics: &Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE service_inventory.service SET characteristics = $2, updated_at = now() WHERE id = $1",
    )
    .bind(id)
    .bind(sqlx::types::Json(characteristics))
    .execute(conn)
    .await?;
    Ok(())
}

pub async fn activate_service(
    conn: &mut PgConnection,
    id: &str,
    at: DateTime<Utc>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE service_inventory.service \
         SET state = 'activated', activated_at = $2, updated_at = now() WHERE id = $1",
    )
    .bind(id)
    .bind(at)
    .execute(conn)
    .await?;
    Ok(())
}

/// The CFS core the task handlers read (locked via `FOR UPDATE` so concurrent
/// `provisioning.task.completed` events serialize on the characteristics RMW —
/// the fix for the lost-update race the Python oracle has).
#[derive(Debug, Clone)]
pub struct ServiceCore {
    pub id: String,
    pub state: String,
    pub characteristics: Value,
}

pub async fn get_service_for_update(
    conn: &mut PgConnection,
    id: &str,
) -> Result<Option<ServiceCore>, sqlx::Error> {
    let row = sqlx::query(
        "SELECT id, state, characteristics FROM service_inventory.service WHERE id = $1 FOR UPDATE",
    )
    .bind(id)
    .fetch_optional(conn)
    .await?;
    Ok(row.map(|r| ServiceCore {
        id: r.get("id"),
        state: r.get("state"),
        characteristics: r
            .try_get::<Option<Value>, _>("characteristics")
            .ok()
            .flatten()
            .unwrap_or_else(|| json!({})),
    }))
}

/// `(id, state)` of each RFS child of a CFS.
pub async fn child_states(
    conn: &mut PgConnection,
    parent_id: &str,
) -> Result<Vec<(String, String)>, sqlx::Error> {
    let rows =
        sqlx::query("SELECT id, state FROM service_inventory.service WHERE parent_service_id = $1")
            .bind(parent_id)
            .fetch_all(conn)
            .await?;
    Ok(rows
        .iter()
        .map(|r| (r.get::<String, _>("id"), r.get::<String, _>("state")))
        .collect())
}

pub async fn add_state_history(
    conn: &mut PgConnection,
    service_id: &str,
    from_state: Option<&str>,
    to_state: &str,
    changed_by: &str,
    reason: &str,
    tenant: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO service_inventory.service_state_history \
         (service_id, from_state, to_state, changed_by, reason, tenant_id) \
         VALUES ($1,$2,$3,$4,$5,$6)",
    )
    .bind(service_id)
    .bind(from_state)
    .bind(to_state)
    .bind(changed_by)
    .bind(reason)
    .bind(tenant)
    .execute(conn)
    .await?;
    Ok(())
}

// ── Reads (TMF response bodies) ───────────────────────────────────────────────

fn service_order_row_to_response(r: &PgRow, items: Vec<Value>) -> Value {
    let id: String = r.get("id");
    json!({
        "id": id,
        "href": format!("{SO_PATH}/{id}"),
        "commercialOrderId": r.get::<String, _>("commercial_order_id"),
        "state": r.get::<String, _>("state"),
        "startedAt": r.get::<Option<DateTime<Utc>>, _>("started_at").map(bss_clock::isoformat),
        "completedAt": r.get::<Option<DateTime<Utc>>, _>("completed_at").map(bss_clock::isoformat),
        "items": items,
        "@type": "ServiceOrder",
    })
}

async fn items_for(pool: &PgPool, service_order_id: &str) -> Result<Vec<Value>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT id, action, service_spec_id, target_service_id \
         FROM service_inventory.service_order_item WHERE service_order_id = $1 ORDER BY id",
    )
    .bind(service_order_id)
    .fetch_all(pool)
    .await?;
    Ok(rows
        .iter()
        .map(|r| {
            json!({
                "id": r.get::<String, _>("id"),
                "action": r.get::<String, _>("action"),
                "serviceSpecId": r.get::<String, _>("service_spec_id"),
                "targetServiceId": r.get::<Option<String>, _>("target_service_id"),
            })
        })
        .collect())
}

pub async fn get_service_order_response(
    pool: &PgPool,
    id: &str,
) -> Result<Option<Value>, sqlx::Error> {
    let row = sqlx::query("SELECT * FROM service_inventory.service_order WHERE id = $1")
        .bind(id)
        .fetch_optional(pool)
        .await?;
    match row {
        Some(r) => {
            let items = items_for(pool, id).await?;
            Ok(Some(service_order_row_to_response(&r, items)))
        }
        None => Ok(None),
    }
}

pub async fn list_service_orders_by_commercial(
    pool: &PgPool,
    commercial_order_id: &str,
) -> Result<Vec<Value>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT * FROM service_inventory.service_order WHERE commercial_order_id = $1 ORDER BY id",
    )
    .bind(commercial_order_id)
    .fetch_all(pool)
    .await?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let id: String = r.get("id");
        let items = items_for(pool, &id).await?;
        out.push(service_order_row_to_response(r, items));
    }
    Ok(out)
}

fn service_row_to_response(r: &PgRow, children: Vec<Value>) -> Value {
    let id: String = r.get("id");
    json!({
        "id": id,
        "href": format!("{SVC_PATH}/{id}"),
        "subscriptionId": r.get::<Option<String>, _>("subscription_id"),
        "specId": r.get::<String, _>("spec_id"),
        "type": r.get::<String, _>("type"),
        "parentServiceId": r.get::<Option<String>, _>("parent_service_id"),
        "state": r.get::<String, _>("state"),
        "characteristics": r.get::<Option<Value>, _>("characteristics"),
        "activatedAt": r.get::<Option<DateTime<Utc>>, _>("activated_at").map(bss_clock::isoformat),
        "terminatedAt": r.get::<Option<DateTime<Utc>>, _>("terminated_at").map(bss_clock::isoformat),
        "children": children,
        "@type": "Service",
    })
}

async fn children_of(pool: &PgPool, parent_id: &str) -> Result<Vec<Value>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT * FROM service_inventory.service WHERE parent_service_id = $1 ORDER BY id",
    )
    .bind(parent_id)
    .fetch_all(pool)
    .await?;
    // v0.1 graph depth is 2 (CFS→RFS); RFS have no children.
    Ok(rows
        .iter()
        .map(|r| service_row_to_response(r, Vec::new()))
        .collect())
}

pub async fn get_service_response(pool: &PgPool, id: &str) -> Result<Option<Value>, sqlx::Error> {
    let row = sqlx::query("SELECT * FROM service_inventory.service WHERE id = $1")
        .bind(id)
        .fetch_optional(pool)
        .await?;
    match row {
        Some(r) => {
            let children = children_of(pool, id).await?;
            Ok(Some(service_row_to_response(&r, children)))
        }
        None => Ok(None),
    }
}

pub async fn list_services_by_subscription(
    pool: &PgPool,
    subscription_id: &str,
) -> Result<Vec<Value>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT * FROM service_inventory.service WHERE subscription_id = $1 ORDER BY id",
    )
    .bind(subscription_id)
    .fetch_all(pool)
    .await?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let id: String = r.get("id");
        let children = children_of(pool, &id).await?;
        out.push(service_row_to_response(r, children));
    }
    Ok(out)
}
