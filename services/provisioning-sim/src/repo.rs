//! `provisioning` schema persistence — port of `task_repo` + `fault_repo`.
//!
//! Raw sqlx. `next_id` draws from `provisioning.task_id_seq` (`PTK-0042`).
//! `fault_injection.probability` is `NUMERIC(3,2)`; we select it as `::float8` so
//! it reads as a native `f64` without pulling in a decimal crate — the Python
//! side does `float(fault.probability)` anyway.

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sqlx::postgres::{PgConnection, PgRow};
use sqlx::{PgPool, Row};

use crate::domain::Task;

pub const TASK_PATH: &str = "/provisioning-api/v1/task";

/// A row of `provisioning.provisioning_task`.
#[derive(Debug, Clone)]
pub struct TaskRow {
    pub id: String,
    pub service_id: String,
    pub task_type: String,
    pub state: String,
    pub attempts: i16,
    pub max_attempts: i16,
    pub payload: Option<Value>,
    pub last_error: Option<String>,
    pub started_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
}

impl TaskRow {
    fn from_row(r: &PgRow) -> Self {
        TaskRow {
            id: r.get("id"),
            service_id: r.get("service_id"),
            task_type: r.get("task_type"),
            state: r.get("state"),
            attempts: r.get("attempts"),
            max_attempts: r.get("max_attempts"),
            payload: r.get("payload"),
            last_error: r.get("last_error"),
            started_at: r.get("started_at"),
            completed_at: r.get("completed_at"),
        }
    }

    /// TMF-style `TaskResponse` body — port of `to_task_response`.
    pub fn to_response(&self) -> Value {
        json!({
            "id": self.id,
            "href": format!("{TASK_PATH}/{}", self.id),
            "serviceId": self.service_id,
            "taskType": self.task_type,
            "state": self.state,
            "attempts": self.attempts,
            "maxAttempts": self.max_attempts,
            "payload": self.payload,
            "lastError": self.last_error,
            "startedAt": self.started_at.map(bss_clock::isoformat),
            "completedAt": self.completed_at.map(bss_clock::isoformat),
            "@type": "ProvisioningTask",
        })
    }
}

/// `SELECT nextval('provisioning.task_id_seq')` → `PTK-0042`.
pub async fn task_next_id(pool: &PgPool) -> Result<String, sqlx::Error> {
    let seq: i64 = sqlx::query_scalar("SELECT nextval('provisioning.task_id_seq')")
        .fetch_one(pool)
        .await?;
    Ok(format!("PTK-{seq:04}"))
}

/// Insert a terminal-state task row (worker persists once, at the end).
pub async fn task_insert(
    conn: &mut PgConnection,
    task: &Task,
    tenant_id: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO provisioning.provisioning_task \
         (id, service_id, task_type, state, attempts, max_attempts, payload, last_error, \
          started_at, completed_at, tenant_id) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
    )
    .bind(&task.id)
    .bind(&task.service_id)
    .bind(&task.task_type)
    .bind(&task.state)
    .bind(task.attempts)
    .bind(task.max_attempts)
    .bind(sqlx::types::Json(&task.payload))
    .bind(&task.last_error)
    .bind(task.started_at)
    .bind(task.completed_at)
    .bind(tenant_id)
    .execute(conn)
    .await?;
    Ok(())
}

/// Update the mutable columns of an existing task row (resolve/retry reset).
pub async fn task_update_state(
    pool: &PgPool,
    id: &str,
    state: &str,
    attempts: i16,
    last_error: Option<&str>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE provisioning.provisioning_task \
         SET state = $2, attempts = $3, last_error = $4, updated_at = now() WHERE id = $1",
    )
    .bind(id)
    .bind(state)
    .bind(attempts)
    .bind(last_error)
    .execute(pool)
    .await?;
    Ok(())
}

pub async fn task_get(pool: &PgPool, id: &str) -> Result<Option<TaskRow>, sqlx::Error> {
    let row = sqlx::query("SELECT * FROM provisioning.provisioning_task WHERE id = $1")
        .bind(id)
        .fetch_optional(pool)
        .await?;
    Ok(row.map(|r| TaskRow::from_row(&r)))
}

pub async fn task_list(
    pool: &PgPool,
    service_id: Option<&str>,
    state: Option<&str>,
) -> Result<Vec<TaskRow>, sqlx::Error> {
    let mut qb = sqlx::QueryBuilder::new("SELECT * FROM provisioning.provisioning_task WHERE 1=1");
    if let Some(sid) = service_id {
        qb.push(" AND service_id = ").push_bind(sid.to_string());
    }
    if let Some(st) = state {
        qb.push(" AND state = ").push_bind(st.to_string());
    }
    qb.push(" ORDER BY created_at DESC");
    let rows = qb.build().fetch_all(pool).await?;
    Ok(rows.iter().map(TaskRow::from_row).collect())
}

/// A row of `provisioning.fault_injection`.
#[derive(Debug, Clone)]
pub struct FaultRow {
    pub id: String,
    pub task_type: String,
    pub fault_type: String,
    pub probability: f64,
    pub enabled: bool,
}

impl FaultRow {
    fn from_row(r: &PgRow) -> Self {
        FaultRow {
            id: r.get("id"),
            task_type: r.get("task_type"),
            fault_type: r.get("fault_type"),
            probability: r.get("probability"),
            enabled: r.get("enabled"),
        }
    }

    pub fn to_response(&self) -> Value {
        json!({
            "id": self.id,
            "taskType": self.task_type,
            "faultType": self.fault_type,
            "probability": self.probability,
            "enabled": self.enabled,
            "@type": "FaultInjection",
        })
    }
}

const FAULT_COLS: &str =
    "id, task_type, fault_type, probability::float8 AS probability, enabled FROM provisioning.fault_injection";

/// The enabled fault rule for `task_type`, if any — port of `get_active_fault`.
pub async fn fault_get_active(
    pool: &PgPool,
    task_type: &str,
) -> Result<Option<FaultRow>, sqlx::Error> {
    let sql = format!("SELECT {FAULT_COLS} WHERE task_type = $1 AND enabled IS TRUE");
    let row = sqlx::query(&sql)
        .bind(task_type)
        .fetch_optional(pool)
        .await?;
    Ok(row.map(|r| FaultRow::from_row(&r)))
}

pub async fn fault_list_all(pool: &PgPool) -> Result<Vec<FaultRow>, sqlx::Error> {
    let sql = format!("SELECT {FAULT_COLS} ORDER BY task_type");
    let rows = sqlx::query(&sql).fetch_all(pool).await?;
    Ok(rows.iter().map(FaultRow::from_row).collect())
}

pub async fn fault_get(pool: &PgPool, id: &str) -> Result<Option<FaultRow>, sqlx::Error> {
    let sql = format!("SELECT {FAULT_COLS} WHERE id = $1");
    let row = sqlx::query(&sql).bind(id).fetch_optional(pool).await?;
    Ok(row.map(|r| FaultRow::from_row(&r)))
}

/// Apply the resolved fault-rule fields and persist — port of `fault_repo.update`.
pub async fn fault_update(pool: &PgPool, fault: &FaultRow) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE provisioning.fault_injection \
         SET enabled = $2, probability = $3, fault_type = $4, updated_at = now() WHERE id = $1",
    )
    .bind(&fault.id)
    .bind(fault.enabled)
    .bind(fault.probability)
    .bind(&fault.fault_type)
    .execute(pool)
    .await?;
    Ok(())
}
