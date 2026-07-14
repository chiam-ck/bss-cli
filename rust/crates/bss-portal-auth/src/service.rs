//! DB-backed session service over the `portal_auth` schema. Port of the
//! session-resolution surface of `bss_portal_auth.service` (`current_session`,
//! `rotate_if_due`, `revoke_session`). The login/step-up write flows land with
//! the P6b auth slice.
//!
//! The cookie value is just the `portal_auth.session` row id. Time comes from
//! [`bss_clock::now`] (deterministic under a frozen clock). Runtime `sqlx::query`
//! + manual row mapping (the workspace pattern — no compile-time DB).

use chrono::{DateTime, Duration, Utc};
use sqlx::{PgPool, Row};

use crate::config::Settings;
use crate::tokens::generate_session_id;
use crate::types::{IdentityView, SessionView};

struct SessionRow {
    id: String,
    identity_id: String,
    issued_at: DateTime<Utc>,
    expires_at: DateTime<Utc>,
    ip: Option<String>,
    user_agent: Option<String>,
    revoked_at: Option<DateTime<Utc>>,
    tenant_id: String,
}

struct IdentityRow {
    id: String,
    email: String,
    customer_id: Option<String>,
    email_verified_at: Option<DateTime<Utc>>,
    status: String,
}

async fn fetch_session(pool: &PgPool, id: &str) -> Result<Option<SessionRow>, sqlx::Error> {
    let row = sqlx::query(
        "SELECT id, identity_id, issued_at, expires_at, ip, user_agent, \
         revoked_at, tenant_id FROM portal_auth.session WHERE id = $1",
    )
    .bind(id)
    .fetch_optional(pool)
    .await?;
    Ok(row.map(|r| SessionRow {
        id: r.get("id"),
        identity_id: r.get("identity_id"),
        issued_at: r.get("issued_at"),
        expires_at: r.get("expires_at"),
        ip: r.get("ip"),
        user_agent: r.get("user_agent"),
        revoked_at: r.get("revoked_at"),
        tenant_id: r.get("tenant_id"),
    }))
}

async fn fetch_identity(pool: &PgPool, id: &str) -> Result<Option<IdentityRow>, sqlx::Error> {
    let row = sqlx::query(
        "SELECT id, email, customer_id, email_verified_at, status \
         FROM portal_auth.identity WHERE id = $1",
    )
    .bind(id)
    .fetch_optional(pool)
    .await?;
    Ok(row.map(|r| IdentityRow {
        id: r.get("id"),
        email: r.get("email"),
        customer_id: r.get("customer_id"),
        email_verified_at: r.get("email_verified_at"),
        status: r.get("status"),
    }))
}

fn session_view(row: &SessionRow, last_seen_at: DateTime<Utc>) -> SessionView {
    SessionView {
        id: row.id.clone(),
        identity_id: row.identity_id.clone(),
        issued_at: row.issued_at,
        expires_at: row.expires_at,
        last_seen_at,
    }
}

fn identity_view(row: &IdentityRow) -> IdentityView {
    IdentityView {
        id: row.id.clone(),
        email: row.email.clone(),
        customer_id: row.customer_id.clone(),
        email_verified_at: row.email_verified_at,
        status: row.status.clone(),
    }
}

/// Resolve a cookie to `(session, identity)` and bump `last_seen_at`. `None` if
/// revoked, expired, the row is missing, or the identity is deleted. Does NOT
/// rotate (that's [`rotate_if_due`]).
pub async fn current_session(
    pool: &PgPool,
    cookie_value: &str,
) -> Result<Option<(SessionView, IdentityView)>, sqlx::Error> {
    if cookie_value.is_empty() {
        return Ok(None);
    }
    let now = bss_clock::now();

    let sess = match fetch_session(pool, cookie_value).await? {
        Some(s) if s.revoked_at.is_none() && s.expires_at > now => s,
        _ => return Ok(None),
    };
    let identity = match fetch_identity(pool, &sess.identity_id).await? {
        Some(i) if i.status != "deleted" => i,
        _ => return Ok(None),
    };

    sqlx::query("UPDATE portal_auth.session SET last_seen_at = $1 WHERE id = $2")
        .bind(now)
        .bind(&sess.id)
        .execute(pool)
        .await?;

    Ok(Some((session_view(&sess, now), identity_view(&identity))))
}

/// If the session has aged past TTL/2, mint a new id + revoke the old (one tx).
/// Returns the NEW session, or `None` when no rotation is due.
pub async fn rotate_if_due(
    pool: &PgPool,
    session_id: &str,
) -> Result<Option<SessionView>, sqlx::Error> {
    let ttl = Settings::from_env().session_ttl_s;
    let now = bss_clock::now();

    let sess = match fetch_session(pool, session_id).await? {
        Some(s) if s.revoked_at.is_none() => s,
        _ => return Ok(None),
    };

    let age = (now - sess.issued_at).num_seconds();
    if (age as f64) < ttl as f64 / 2.0 {
        return Ok(None);
    }

    let new_id = generate_session_id();
    let expires_at = now + Duration::seconds(ttl);

    let mut tx = pool.begin().await?;
    sqlx::query(
        "INSERT INTO portal_auth.session \
         (id, identity_id, issued_at, expires_at, last_seen_at, ip, user_agent, tenant_id) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
    )
    .bind(&new_id)
    .bind(&sess.identity_id)
    .bind(now)
    .bind(expires_at)
    .bind(now)
    .bind(&sess.ip)
    .bind(&sess.user_agent)
    .bind(&sess.tenant_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query("UPDATE portal_auth.session SET revoked_at = $1 WHERE id = $2")
        .bind(now)
        .bind(session_id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;

    Ok(Some(SessionView {
        id: new_id,
        identity_id: sess.identity_id,
        issued_at: now,
        expires_at,
        last_seen_at: now,
    }))
}

/// Explicit logout — set `revoked_at = now()`. Idempotent.
pub async fn revoke_session(pool: &PgPool, session_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE portal_auth.session SET revoked_at = $1 \
         WHERE id = $2 AND revoked_at IS NULL",
    )
    .bind(bss_clock::now())
    .bind(session_id)
    .execute(pool)
    .await?;
    Ok(())
}
