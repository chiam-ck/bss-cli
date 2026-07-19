//! Portal-side audit helper — append a `portal_auth.portal_action` row. Port of
//! `bss_portal_auth.audit.record_portal_action`.
//!
//! Every direct post-login self-serve write records one row here after the BSS
//! write resolves (success or failure). Doctrine: write success AND failure
//! paths — a flurry of failures on a single customer is a compromise signal.
//! `ts` comes from [`bss_clock::now`]; `tenant_id` is left to the schema
//! server-default (`DEFAULT`), matching the session/login-attempt inserts.

use sqlx::PgPool;

/// The resolved primitives for one `portal_action` row. Mirrors the keyword
/// arguments of the Python helper so the caller stays FastAPI-free.
#[derive(Debug, Clone, Default)]
pub struct PortalActionRecord<'a> {
    pub customer_id: Option<&'a str>,
    pub identity_id: Option<&'a str>,
    pub action: &'a str,
    pub route: &'a str,
    pub method: &'a str,
    pub success: bool,
    pub error_rule: Option<&'a str>,
    pub step_up_consumed: bool,
    pub ip: Option<&'a str>,
    pub user_agent: Option<&'a str>,
}

/// Append a `portal_action` row. One INSERT (autocommitted on the pool); the
/// Python version flushes inside the caller's transaction, but the row is
/// independent of the BSS write so a standalone commit is equivalent.
pub async fn record_portal_action(
    pool: &PgPool,
    rec: &PortalActionRecord<'_>,
) -> Result<(), sqlx::Error> {
    let ts = bss_clock::now();
    sqlx::query(
        "INSERT INTO portal_auth.portal_action \
         (ts, customer_id, identity_id, action, route, method, success, \
          error_rule, step_up_consumed, ip, user_agent) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
    )
    .bind(ts)
    .bind(rec.customer_id)
    .bind(rec.identity_id)
    .bind(rec.action)
    .bind(rec.route)
    .bind(rec.method)
    .bind(rec.success)
    .bind(rec.error_rule)
    .bind(rec.step_up_consumed)
    .bind(rec.ip)
    .bind(rec.user_agent)
    .execute(pool)
    .await?;
    Ok(())
}
