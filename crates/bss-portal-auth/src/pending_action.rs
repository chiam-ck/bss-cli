//! POST-body stash so a step-up bounce doesn't lose the customer's typed input.
//! Port of `bss_portal_auth.pending_action`.
//!
//! When `requires_step_up` bounces a sensitive POST to `/auth/step-up`, the
//! route handler never ran — but the customer already typed their values. On
//! bounce the portal stashes the (filtered) form payload; on step-up success it
//! consumes the stash and renders an auto-replay form that re-POSTs with the
//! fresh grant cookie. A partial unique index enforces one in-flight stash per
//! `(session, action_label)`; a new stash supersedes the prior unconsumed row.

use chrono::{DateTime, Duration, Utc};
use serde_json::{Map, Value};
use sqlx::{PgPool, Row};

use crate::config::Settings;

/// Form fields never stashed — auth-flow internals, stale on replay.
const STRIP_FIELDS: &[&str] = &["step_up_token"];

/// Read-only projection of a stashed pending action.
#[derive(Debug, Clone)]
pub struct PendingActionView {
    pub id: String,
    pub session_id: String,
    pub action_label: String,
    pub target_url: String,
    /// The replay form fields (string → string), filtered of auth internals.
    pub payload: Vec<(String, String)>,
    pub expires_at: DateTime<Utc>,
}

/// Why a stash failed.
#[derive(Debug)]
pub enum StashError {
    /// Session missing or revoked — nothing could consume the stash.
    SessionInvalid,
    Db(sqlx::Error),
}

impl std::fmt::Display for StashError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StashError::SessionInvalid => write!(f, "session not found or revoked"),
            StashError::Db(e) => write!(f, "db error: {e}"),
        }
    }
}
impl std::error::Error for StashError {}
impl From<sqlx::Error> for StashError {
    fn from(e: sqlx::Error) -> Self {
        StashError::Db(e)
    }
}

fn pending_id() -> String {
    use rand::rngs::OsRng;
    use rand::RngCore;
    let mut bytes = [0u8; 8];
    OsRng.fill_bytes(&mut bytes);
    let mut hex = String::with_capacity(16);
    for b in bytes {
        hex.push_str(&format!("{b:02x}"));
    }
    format!("SUP-{hex}")
}

/// Stash a POST body for replay after step-up verification. Supersedes any prior
/// unconsumed row for `(session_id, action_label)`. Returns the new row id.
pub async fn stash_pending_action(
    pool: &PgPool,
    session_id: &str,
    action_label: &str,
    target_url: &str,
    payload: &[(String, String)],
    ttl_s: Option<i64>,
) -> Result<String, StashError> {
    // A stash is pointless for a dead session.
    let active = sqlx::query("SELECT revoked_at FROM portal_auth.session WHERE id = $1")
        .bind(session_id)
        .fetch_optional(pool)
        .await?;
    match active {
        Some(r) if r.get::<Option<DateTime<Utc>>, _>("revoked_at").is_none() => {}
        _ => return Err(StashError::SessionInvalid),
    }

    let now = bss_clock::now();
    let ttl = ttl_s.unwrap_or_else(|| Settings::from_env().stepup_pending_ttl_s);
    let expires = now + Duration::seconds(ttl);

    let mut obj = Map::new();
    for (k, v) in payload {
        if !STRIP_FIELDS.contains(&k.as_str()) {
            obj.insert(k.clone(), Value::String(v.clone()));
        }
    }
    let payload_json = Value::Object(obj);

    let mut tx = pool.begin().await?;
    // Supersede any prior in-flight stash (clears the partial unique index).
    sqlx::query(
        "UPDATE portal_auth.step_up_pending_action SET consumed_at = $1 \
         WHERE session_id = $2 AND action_label = $3 AND consumed_at IS NULL",
    )
    .bind(now)
    .bind(session_id)
    .bind(action_label)
    .execute(&mut *tx)
    .await?;

    let row_id = pending_id();
    sqlx::query(
        "INSERT INTO portal_auth.step_up_pending_action \
         (id, session_id, action_label, target_url, payload_json, created_at, expires_at) \
         VALUES ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(&row_id)
    .bind(session_id)
    .bind(action_label)
    .bind(target_url)
    .bind(&payload_json)
    .bind(now)
    .bind(expires)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(row_id)
}

/// Atomically take the most recent unconsumed, unexpired stash for the key.
/// Marks the row consumed (one-shot). Port of `consume_pending_action`.
pub async fn consume_pending_action(
    pool: &PgPool,
    session_id: &str,
    action_label: &str,
) -> Result<Option<PendingActionView>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT id, session_id, action_label, target_url, payload_json, expires_at \
         FROM portal_auth.step_up_pending_action \
         WHERE session_id = $1 AND action_label = $2 AND consumed_at IS NULL",
    )
    .bind(session_id)
    .bind(action_label)
    .fetch_all(pool)
    .await?;

    let now = bss_clock::now();
    for row in &rows {
        let expires: DateTime<Utc> = row.get("expires_at");
        if expires <= now {
            continue;
        }
        let id: String = row.get("id");
        sqlx::query("UPDATE portal_auth.step_up_pending_action SET consumed_at = $1 WHERE id = $2")
            .bind(now)
            .bind(&id)
            .execute(pool)
            .await?;
        let payload_json: Value = row.get("payload_json");
        let payload = payload_json
            .as_object()
            .map(|m| {
                m.iter()
                    .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string())))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        return Ok(Some(PendingActionView {
            id,
            session_id: row.get("session_id"),
            action_label: row.get("action_label"),
            target_url: row.get("target_url"),
            payload,
            expires_at: expires,
        }));
    }
    Ok(None)
}
