//! Admin reset router — per-service operational-data wipe.
//!
//! Port of `bss_admin.reset`. Mounted under `/admin-api/v1`, exposing
//! `POST /reset-operational-data`. Gated behind `BSS_ALLOW_ADMIN_RESET`; wipes
//! each declared table (truncate, or a fixed update for reference-backed pools)
//! and writes an `admin.operational_data_reset` audit marker so scenarios can
//! filter on `occurred_at >= resetAt` rather than truncating audit history.

use axum::{
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::post,
    Json, Router,
};
use bss_db::PgPool;
use serde_json::{json, Value};

/// How to reset a single table.
#[derive(Debug, Clone)]
pub struct TableReset {
    /// Bare table name (quoted `"schema"."name"` by the handler).
    pub name: String,
    pub mode: ResetMode,
}

/// Reset strategy for one table.
#[derive(Debug, Clone)]
pub enum ResetMode {
    /// `TRUNCATE TABLE ... RESTART IDENTITY CASCADE` — wipe every row.
    Truncate,
    /// A fixed `UPDATE` restoring rows to an available/default state — used for
    /// reference-backed pools (e.g. `inventory.msisdn_pool`) where the rows are
    /// kept but their assignment cleared. Carries the exact SQL.
    Update(String),
}

impl TableReset {
    /// A truncate-mode reset for `name`.
    pub fn truncate(name: impl Into<String>) -> Self {
        TableReset {
            name: name.into(),
            mode: ResetMode::Truncate,
        }
    }

    /// An update-mode reset running `update_sql`.
    pub fn update(name: impl Into<String>, update_sql: impl Into<String>) -> Self {
        TableReset {
            name: name.into(),
            mode: ResetMode::Update(update_sql.into()),
        }
    }
}

/// One schema's admin reset manifest. `schema` is the Postgres schema the service
/// owns; every table in `tables` is prefixed with it.
#[derive(Debug, Clone)]
pub struct ResetPlan {
    pub schema: String,
    pub tables: Vec<TableReset>,
}

impl ResetPlan {
    /// Build a plan for `schema` covering `tables`.
    pub fn new(schema: impl Into<String>, tables: Vec<TableReset>) -> Self {
        ResetPlan {
            schema: schema.into(),
            tables,
        }
    }
}

#[derive(Clone)]
struct AdminState {
    pool: PgPool,
    service_name: String,
    plans: std::sync::Arc<Vec<ResetPlan>>,
}

/// Build the `/reset-operational-data` router for one service. `service_name`
/// tags the audit marker and response body; `plans` lists every schema the
/// service owns. Mount under `/admin-api/v1`.
pub fn admin_reset_router(
    pool: PgPool,
    service_name: impl Into<String>,
    plans: Vec<ResetPlan>,
) -> Router {
    let state = AdminState {
        pool,
        service_name: service_name.into(),
        plans: std::sync::Arc::new(plans),
    };
    Router::new()
        .route("/reset-operational-data", post(reset_operational_data))
        .with_state(state)
}

/// Whether the reset flag is set (read per-request, matching Python
/// `os.environ.get` — the flag is a non-secret preference, not a startup token).
fn is_allowed() -> bool {
    matches!(
        std::env::var("BSS_ALLOW_ADMIN_RESET")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

/// A reset failure rendered as an HTTP response.
enum AdminError {
    Disabled,
    Db(sqlx::Error),
}

impl IntoResponse for AdminError {
    fn into_response(self) -> Response {
        match self {
            AdminError::Disabled => (
                StatusCode::FORBIDDEN,
                Json(json!({
                    "detail": {
                        "code": "ADMIN_RESET_DISABLED",
                        "message": "Admin reset is gated behind the BSS_ALLOW_ADMIN_RESET \
                                    env flag. Set it to 'true' in the service environment \
                                    (scenario runs and developer REPLs only).",
                    }
                })),
            )
                .into_response(),
            AdminError::Db(e) => {
                tracing::error!(error = %e, "admin.reset.db_error");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({ "detail": "admin reset failed" })),
                )
                    .into_response()
            }
        }
    }
}

async fn reset_operational_data(
    State(s): State<AdminState>,
    headers: HeaderMap,
) -> Result<Json<Value>, AdminError> {
    if !is_allowed() {
        return Err(AdminError::Disabled);
    }

    let started_at = bss_clock::now();
    let mut tx = s.pool.begin().await.map_err(AdminError::Db)?;

    let mut per_schema: Vec<Value> = Vec::new();
    for plan in s.plans.iter() {
        let mut truncated: Vec<String> = Vec::new();
        let mut updated: Vec<String> = Vec::new();
        for table in &plan.tables {
            match &table.mode {
                ResetMode::Truncate => {
                    let sql = format!(
                        "TRUNCATE TABLE \"{}\".\"{}\" RESTART IDENTITY CASCADE",
                        plan.schema, table.name
                    );
                    sqlx::query(&sql)
                        .execute(&mut *tx)
                        .await
                        .map_err(AdminError::Db)?;
                    truncated.push(table.name.clone());
                }
                ResetMode::Update(update_sql) => {
                    sqlx::query(update_sql)
                        .execute(&mut *tx)
                        .await
                        .map_err(AdminError::Db)?;
                    updated.push(table.name.clone());
                }
            }
        }
        per_schema.push(json!({
            "schema": plan.schema,
            "truncated": truncated,
            "updated": updated,
        }));
    }

    // Audit marker — same columns/shape as the Python router (no service_identity;
    // the column default fills it).
    let actor = header_str(&headers, "x-bss-actor", "system");
    let channel = header_str(&headers, "x-bss-channel", "cli");
    let payload = json!({ "service": s.service_name, "schemas": per_schema });
    sqlx::query(
        "INSERT INTO audit.domain_event \
         (event_id, event_type, aggregate_type, aggregate_id, occurred_at, actor, channel, \
          tenant_id, payload, schema_version, published_to_mq) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,1,false)",
    )
    .bind(uuid::Uuid::new_v4())
    .bind("admin.operational_data_reset")
    .bind("service")
    .bind(&s.service_name)
    .bind(started_at)
    .bind(&actor)
    .bind(&channel)
    .bind("DEFAULT")
    .bind(sqlx::types::Json(payload))
    .execute(&mut *tx)
    .await
    .map_err(AdminError::Db)?;

    tx.commit().await.map_err(AdminError::Db)?;

    tracing::warn!(service = %s.service_name, "admin.reset.completed");
    Ok(Json(json!({
        "service": s.service_name,
        "schemas": per_schema,
        "resetAt": started_at.to_rfc3339(),
    })))
}

fn header_str(headers: &HeaderMap, name: &str, default: &str) -> String {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .filter(|v| !v.is_empty())
        .unwrap_or(default)
        .to_string()
}
