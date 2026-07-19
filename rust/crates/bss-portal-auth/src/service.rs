//! DB-backed session service over the `portal_auth` schema. Port of the
//! session-resolution surface of `bss_portal_auth.service` (`current_session`,
//! `rotate_if_due`, `revoke_session`). The login/step-up write flows land with
//! the P6b auth slice.
//!
//! The cookie value is just the `portal_auth.session` row id. Time comes from
//! [`bss_clock::now`] (deterministic under a frozen clock). Runtime `sqlx::query`
//! + manual row mapping (the workspace pattern — no compile-time DB).

use chrono::{DateTime, Duration, Utc};
use rand::rngs::OsRng;
use rand::RngCore;
use sqlx::{PgPool, Row};

use crate::config::Settings;
use crate::email::EmailAdapter;
use crate::tokens::{
    generate_magic_link_token, generate_otp, generate_session_id, generate_step_up_grant,
    hash_token, verify_token,
};
use crate::types::{
    IdentityView, LoginChallenge, LoginFailed, RateLimitExceeded, SessionView, StepUpChallenge,
    StepUpFailed, StepUpToken,
};

/// A login-flow error: either a DB failure or a rate-limit trip.
#[derive(Debug)]
pub enum LoginError {
    Db(sqlx::Error),
    RateLimited(RateLimitExceeded),
}

impl std::fmt::Display for LoginError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LoginError::Db(e) => write!(f, "db error: {e}"),
            LoginError::RateLimited(e) => write!(f, "{e}"),
        }
    }
}
impl std::error::Error for LoginError {}
impl From<sqlx::Error> for LoginError {
    fn from(e: sqlx::Error) -> Self {
        LoginError::Db(e)
    }
}

/// The outcome of a verify: a fresh session, or a structured failure (the portal
/// renders a generic message; `reason` is for the audit log).
pub enum VerifyOutcome {
    Session(SessionView),
    Failed(LoginFailed),
}

fn hex_id(prefix: &str) -> String {
    let mut bytes = [0u8; 8];
    OsRng.fill_bytes(&mut bytes);
    let mut hex = String::with_capacity(16);
    for b in bytes {
        hex.push_str(&format!("{b:02x}"));
    }
    format!("{prefix}-{hex}")
}

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

// ── rate limits (portal_auth.login_attempt window counts) ────────────────────

async fn count_in_window(
    pool: &PgPool,
    col: &str,
    value: &str,
    stage: &str,
    window_s: i64,
    until: DateTime<Utc>,
) -> Result<(i64, Option<DateTime<Utc>>), sqlx::Error> {
    let cutoff = until - Duration::seconds(window_s);
    // `col` is a fixed identifier ("email" / "ip"), never user input.
    let sql = format!(
        "SELECT count(id) AS c, min(ts) AS oldest FROM portal_auth.login_attempt \
         WHERE {col} = $1 AND stage = $2 AND ts >= $3"
    );
    let row = sqlx::query(&sql)
        .bind(value)
        .bind(stage)
        .bind(cutoff)
        .fetch_one(pool)
        .await?;
    Ok((row.get::<i64, _>("c"), row.get("oldest")))
}

fn retry_after_s(oldest: Option<DateTime<Utc>>, now: DateTime<Utc>, window_s: i64) -> i64 {
    match oldest {
        None => window_s,
        Some(o) => (window_s - (now - o).num_seconds()).max(1),
    }
}

async fn enforce_login_start(
    pool: &PgPool,
    email: &str,
    ip: Option<&str>,
) -> Result<(), LoginError> {
    let s = Settings::from_env();
    let now = bss_clock::now();
    let (ce, oe) = count_in_window(
        pool,
        "email",
        email,
        "login_start",
        s.login_per_email_window_s,
        now,
    )
    .await?;
    if ce >= s.login_per_email_max {
        return Err(LoginError::RateLimited(RateLimitExceeded {
            retry_after_seconds: retry_after_s(oe, now, s.login_per_email_window_s),
            scope: "login_start_per_email".to_string(),
        }));
    }
    if let Some(ip) = ip {
        let (ci, oi) =
            count_in_window(pool, "ip", ip, "login_start", s.login_per_ip_window_s, now).await?;
        if ci >= s.login_per_ip_max {
            return Err(LoginError::RateLimited(RateLimitExceeded {
                retry_after_seconds: retry_after_s(oi, now, s.login_per_ip_window_s),
                scope: "login_start_per_ip".to_string(),
            }));
        }
    }
    Ok(())
}

async fn enforce_login_verify(pool: &PgPool, email: &str) -> Result<(), LoginError> {
    let s = Settings::from_env();
    let now = bss_clock::now();
    let (c, o) = count_in_window(
        pool,
        "email",
        email,
        "login_verify",
        s.verify_per_email_window_s,
        now,
    )
    .await?;
    if c >= s.verify_per_email_max {
        return Err(LoginError::RateLimited(RateLimitExceeded {
            retry_after_seconds: retry_after_s(o, now, s.verify_per_email_window_s),
            scope: "login_verify_per_email".to_string(),
        }));
    }
    Ok(())
}

// ── magic-link URL ───────────────────────────────────────────────────────────

fn build_magic_link_url(public_url: &str, email: &str, token: &str) -> String {
    if public_url.is_empty() {
        return token.to_string();
    }
    let base = public_url.trim_end_matches('/');
    format!(
        "{base}/auth/verify?email={}&token={}",
        urlencode(email),
        urlencode(token)
    )
}

/// Minimal `urllib.parse.quote` (default safe='/'): percent-encode everything
/// that isn't unreserved or `/`. Enough for emails + url-safe tokens.
fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        let keep = b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~' | b'/');
        if keep {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

// ── start_email_login ────────────────────────────────────────────────────────

/// Mint OTP + magic-link, store hashed, hand off to the email adapter. Idempotent
/// on the identity (a known email re-uses its row).
pub async fn start_email_login(
    pool: &PgPool,
    email: &str,
    ip: Option<&str>,
    user_agent: Option<&str>,
    email_adapter: &dyn EmailAdapter,
) -> Result<LoginChallenge, LoginError> {
    let s = Settings::from_env();
    enforce_login_start(pool, email, ip).await?;

    let now = bss_clock::now();
    let mut tx = pool.begin().await?;

    // Identity: reuse or create.
    let existing: Option<String> =
        sqlx::query("SELECT id FROM portal_auth.identity WHERE email = $1")
            .bind(email)
            .fetch_optional(&mut *tx)
            .await?
            .map(|r| r.get("id"));
    let identity_id = match existing {
        Some(id) => id,
        None => {
            let id = hex_id("IDN");
            sqlx::query(
                "INSERT INTO portal_auth.identity (id, email, status, created_at) \
                 VALUES ($1, $2, 'unverified', $3)",
            )
            .bind(&id)
            .bind(email)
            .bind(now)
            .execute(&mut *tx)
            .await?;
            id
        }
    };

    let otp = generate_otp();
    let magic = generate_magic_link_token();
    let expires = now + Duration::seconds(s.login_token_ttl_s);

    #[allow(clippy::expect_used)]
    for (kind, code) in [("otp", &otp), ("magic_link", &magic)] {
        let hash = hash_token(code, None).expect("pepper validated at startup");
        sqlx::query(
            "INSERT INTO portal_auth.login_token \
             (id, identity_id, kind, code_hash, issued_at, expires_at, ip, user_agent) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        )
        .bind(hex_id("LTK"))
        .bind(&identity_id)
        .bind(kind)
        .bind(&hash)
        .bind(now)
        .bind(expires)
        .bind(ip)
        .bind(user_agent)
        .execute(&mut *tx)
        .await?;
    }

    record_attempt(&mut tx, Some(email), ip, "login_start", "issued").await?;
    tx.commit().await?;

    let url = build_magic_link_url(&s.public_url, email, &magic);
    email_adapter.send_login(email, &otp, &url);

    Ok(LoginChallenge {
        identity_id,
        expires_at: expires,
    })
}

async fn record_attempt(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    email: Option<&str>,
    ip: Option<&str>,
    stage: &str,
    outcome: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO portal_auth.login_attempt (email, ip, ts, outcome, stage) \
         VALUES ($1, $2, $3, $4, $5)",
    )
    .bind(email)
    .bind(ip)
    .bind(bss_clock::now())
    .bind(outcome)
    .bind(stage)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

// ── verify_email_login ───────────────────────────────────────────────────────

/// Verify OTP or magic-link `code` against active tokens for `email`. On success:
/// consume the token, mint a session, stamp `email_verified_at` (first time),
/// auto-link to a matching CRM customer, update `last_login_at`.
pub async fn verify_email_login(
    pool: &PgPool,
    email: &str,
    code: &str,
    ip: Option<&str>,
    user_agent: Option<&str>,
) -> Result<VerifyOutcome, LoginError> {
    let s = Settings::from_env();
    enforce_login_verify(pool, email).await?;

    let now = bss_clock::now();
    let mut tx = pool.begin().await?;

    let ident = sqlx::query(
        "SELECT id, customer_id, email_verified_at, status FROM portal_auth.identity \
         WHERE email = $1",
    )
    .bind(email)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(ident) = ident else {
        record_attempt(&mut tx, Some(email), ip, "login_verify", "no_such_identity").await?;
        tx.commit().await?;
        return Ok(VerifyOutcome::Failed(LoginFailed {
            reason: "no_such_identity".to_string(),
        }));
    };
    let identity_id: String = ident.get("id");
    let mut customer_id: Option<String> = ident.get("customer_id");
    let email_verified_at: Option<DateTime<Utc>> = ident.get("email_verified_at");
    let status: String = ident.get("status");

    let rows = sqlx::query(
        "SELECT id, code_hash, expires_at FROM portal_auth.login_token \
         WHERE identity_id = $1 AND kind IN ('otp','magic_link') AND consumed_at IS NULL",
    )
    .bind(&identity_id)
    .fetch_all(&mut *tx)
    .await?;

    if rows.is_empty() {
        record_attempt(&mut tx, Some(email), ip, "login_verify", "no_active_token").await?;
        tx.commit().await?;
        return Ok(VerifyOutcome::Failed(LoginFailed {
            reason: "no_active_token".to_string(),
        }));
    }

    let mut matched: Option<String> = None;
    let mut any_unexpired = false;
    for row in &rows {
        let expires: DateTime<Utc> = row.get("expires_at");
        if expires <= now {
            continue;
        }
        any_unexpired = true;
        let hash: String = row.get("code_hash");
        if verify_token(code, &hash, None) {
            matched = Some(row.get("id"));
            break;
        }
    }

    let Some(token_id) = matched else {
        let outcome = if any_unexpired {
            "wrong_code"
        } else {
            "expired"
        };
        record_attempt(&mut tx, Some(email), ip, "login_verify", outcome).await?;
        tx.commit().await?;
        return Ok(VerifyOutcome::Failed(LoginFailed {
            reason: outcome.to_string(),
        }));
    };

    sqlx::query("UPDATE portal_auth.login_token SET consumed_at = $1 WHERE id = $2")
        .bind(now)
        .bind(&token_id)
        .execute(&mut *tx)
        .await?;

    // Auto-link to a pre-existing CRM customer by email (unique contact medium).
    if customer_id.is_none() {
        let cm = sqlx::query(
            "SELECT party_id FROM crm.contact_medium \
             WHERE medium_type = 'email' AND value = $1 AND valid_to IS NULL",
        )
        .bind(email)
        .fetch_optional(&mut *tx)
        .await?;
        if let Some(cm) = cm {
            let party_id: String = cm.get("party_id");
            let cust = sqlx::query("SELECT id FROM crm.customer WHERE party_id = $1")
                .bind(&party_id)
                .fetch_optional(&mut *tx)
                .await?;
            if let Some(cust) = cust {
                customer_id = Some(cust.get("id"));
            }
        }
    }

    // Status transitions mirroring the oracle.
    let new_status: &str = if email_verified_at.is_none() {
        if customer_id.is_some() {
            "registered"
        } else {
            "verified"
        }
    } else if customer_id.is_some() && status != "registered" {
        "registered"
    } else {
        status.as_str()
    };
    let verified_at = email_verified_at.or(Some(now));

    sqlx::query(
        "UPDATE portal_auth.identity \
         SET customer_id = $1, email_verified_at = $2, status = $3, last_login_at = $4 \
         WHERE id = $5",
    )
    .bind(&customer_id)
    .bind(verified_at)
    .bind(new_status)
    .bind(now)
    .bind(&identity_id)
    .execute(&mut *tx)
    .await?;

    let session_id = generate_session_id();
    let expires_at = now + Duration::seconds(s.session_ttl_s);
    sqlx::query(
        "INSERT INTO portal_auth.session \
         (id, identity_id, issued_at, expires_at, last_seen_at, ip, user_agent) \
         VALUES ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(&session_id)
    .bind(&identity_id)
    .bind(now)
    .bind(expires_at)
    .bind(now)
    .bind(ip)
    .bind(user_agent)
    .execute(&mut *tx)
    .await?;

    record_attempt(&mut tx, Some(email), ip, "login_verify", "success").await?;
    tx.commit().await?;

    Ok(VerifyOutcome::Session(SessionView {
        id: session_id,
        identity_id,
        issued_at: now,
        expires_at,
        last_seen_at: now,
    }))
}

// ── link_to_customer ─────────────────────────────────────────────────────────

/// Why binding an identity to a customer failed. `AlreadyLinked` carries the
/// customer id the identity is currently bound to (the signup route logs it).
#[derive(Debug)]
pub enum LinkError {
    UnknownIdentity,
    AlreadyLinked { existing: String },
    Db(sqlx::Error),
}

impl std::fmt::Display for LinkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LinkError::UnknownIdentity => write!(f, "unknown identity"),
            LinkError::AlreadyLinked { existing } => {
                write!(f, "identity already linked to customer {existing}")
            }
            LinkError::Db(e) => write!(f, "db error: {e}"),
        }
    }
}

impl std::error::Error for LinkError {}

impl From<sqlx::Error> for LinkError {
    fn from(e: sqlx::Error) -> Self {
        LinkError::Db(e)
    }
}

/// Bind an identity to a customer at the moment of first paid signup. Port of
/// `bss_portal_auth.service.link_to_customer`.
///
/// Idempotent: re-calling with the same `(identity, customer)` pair is a no-op.
/// Calling with a different customer when one is already linked is a
/// [`LinkError::AlreadyLinked`] — links are 1:1 and not reassignable from this
/// surface.
pub async fn link_to_customer(
    pool: &PgPool,
    identity_id: &str,
    customer_id: &str,
) -> Result<(), LinkError> {
    let existing: Option<Option<String>> =
        sqlx::query("SELECT customer_id FROM portal_auth.identity WHERE id = $1")
            .bind(identity_id)
            .fetch_optional(pool)
            .await?
            .map(|r| r.get("customer_id"));

    match existing {
        None => Err(LinkError::UnknownIdentity),
        Some(Some(current)) if current == customer_id => Ok(()),
        Some(Some(current)) => Err(LinkError::AlreadyLinked { existing: current }),
        Some(None) => {
            sqlx::query(
                "UPDATE portal_auth.identity \
                 SET customer_id = $1, status = 'registered' WHERE id = $2",
            )
            .bind(customer_id)
            .bind(identity_id)
            .execute(pool)
            .await?;
            Ok(())
        }
    }
}

// ── step-up: start / verify / consume ────────────────────────────────────────

/// Why a step-up start failed.
#[derive(Debug)]
pub enum StepUpError {
    RateLimited(RateLimitExceeded),
    /// Session missing or revoked (or its identity is gone).
    SessionInvalid,
    Db(sqlx::Error),
}

impl std::fmt::Display for StepUpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StepUpError::RateLimited(e) => write!(f, "rate limited: {e}"),
            StepUpError::SessionInvalid => write!(f, "session not found or revoked"),
            StepUpError::Db(e) => write!(f, "db error: {e}"),
        }
    }
}

impl std::error::Error for StepUpError {}

impl From<sqlx::Error> for StepUpError {
    fn from(e: sqlx::Error) -> Self {
        StepUpError::Db(e)
    }
}

/// Outcome of [`verify_step_up`].
pub enum StepUpVerify {
    Token(StepUpToken),
    Failed(StepUpFailed),
}

/// Issue a fresh OTP scoped to `action_label`. Port of `start_step_up`.
pub async fn start_step_up(
    pool: &PgPool,
    session_id: &str,
    action_label: &str,
    ip: Option<&str>,
    user_agent: Option<&str>,
    email_adapter: &dyn EmailAdapter,
) -> Result<StepUpChallenge, StepUpError> {
    let s = Settings::from_env();
    let now = bss_clock::now();

    // Per-session cap — keyed on the `ip` column via `session:<id>` (matches the
    // Python reuse of the flat login_attempt log).
    let session_key = format!("session:{session_id}");
    let (count, oldest) = count_in_window(
        pool,
        "ip",
        &session_key,
        "step_up_start",
        s.stepup_per_session_window_s,
        now,
    )
    .await?;
    if count >= s.stepup_per_session_max {
        let retry = retry_after_s(oldest, now, s.stepup_per_session_window_s);
        return Err(StepUpError::RateLimited(RateLimitExceeded {
            retry_after_seconds: retry,
            scope: "step_up_per_session".to_string(),
        }));
    }

    let sess = sqlx::query("SELECT identity_id, revoked_at FROM portal_auth.session WHERE id = $1")
        .bind(session_id)
        .fetch_optional(pool)
        .await?;
    let Some(sess) = sess else {
        return Err(StepUpError::SessionInvalid);
    };
    if sess.get::<Option<DateTime<Utc>>, _>("revoked_at").is_some() {
        return Err(StepUpError::SessionInvalid);
    }
    let identity_id: String = sess.get("identity_id");
    let email: Option<String> = sqlx::query("SELECT email FROM portal_auth.identity WHERE id = $1")
        .bind(&identity_id)
        .fetch_optional(pool)
        .await?
        .map(|r| r.get("email"));
    let Some(email) = email else {
        return Err(StepUpError::SessionInvalid);
    };

    let otp = generate_otp();
    let expires = now + Duration::seconds(s.stepup_token_ttl_s);
    #[allow(clippy::expect_used)]
    let hash = hash_token(&otp, None).expect("pepper validated at startup");

    let mut tx = pool.begin().await?;
    sqlx::query(
        "INSERT INTO portal_auth.login_token \
         (id, identity_id, kind, code_hash, action_label, issued_at, expires_at, ip, user_agent) \
         VALUES ($1, $2, 'step_up', $3, $4, $5, $6, $7, $8)",
    )
    .bind(hex_id("LTK"))
    .bind(&identity_id)
    .bind(&hash)
    .bind(action_label)
    .bind(now)
    .bind(expires)
    .bind(ip)
    .bind(user_agent)
    .execute(&mut *tx)
    .await?;
    record_attempt(
        &mut tx,
        Some(&email),
        Some(&session_key),
        "step_up_start",
        "issued",
    )
    .await?;
    tx.commit().await?;

    email_adapter.send_step_up(&email, &otp, action_label);

    Ok(StepUpChallenge {
        session_id: session_id.to_string(),
        action_label: action_label.to_string(),
        expires_at: expires,
    })
}

/// Match an OTP against an active `step_up` token scoped to `action_label`. On
/// success, consume the OTP and mint a one-shot `step_up_grant`. Port of
/// `verify_step_up`.
pub async fn verify_step_up(
    pool: &PgPool,
    session_id: &str,
    code: &str,
    action_label: &str,
) -> Result<StepUpVerify, sqlx::Error> {
    let s = Settings::from_env();
    let now = bss_clock::now();

    let Some(sess) = session_identity_if_active(pool, session_id).await? else {
        return Ok(StepUpVerify::Failed(StepUpFailed {
            reason: "no_active_token".to_string(),
        }));
    };

    let rows = sqlx::query(
        "SELECT id, code_hash, expires_at FROM portal_auth.login_token \
         WHERE identity_id = $1 AND kind = 'step_up' AND action_label = $2 \
           AND consumed_at IS NULL",
    )
    .bind(&sess)
    .bind(action_label)
    .fetch_all(pool)
    .await?;
    if rows.is_empty() {
        return Ok(StepUpVerify::Failed(StepUpFailed {
            reason: "no_active_token".to_string(),
        }));
    }

    let mut matched_id: Option<String> = None;
    let mut any_unexpired = false;
    for row in &rows {
        let expires: DateTime<Utc> = row.get("expires_at");
        if expires <= now {
            continue;
        }
        any_unexpired = true;
        let hash: String = row.get("code_hash");
        if verify_token(code, &hash, None) {
            matched_id = Some(row.get("id"));
            break;
        }
    }

    let Some(matched_id) = matched_id else {
        return Ok(StepUpVerify::Failed(StepUpFailed {
            reason: if any_unexpired {
                "wrong_code"
            } else {
                "expired"
            }
            .to_string(),
        }));
    };

    let grant = generate_step_up_grant();
    let grant_expires = now + Duration::seconds(s.stepup_grant_ttl_s);
    #[allow(clippy::expect_used)]
    let grant_hash = hash_token(&grant, None).expect("pepper validated at startup");

    let mut tx = pool.begin().await?;
    sqlx::query("UPDATE portal_auth.login_token SET consumed_at = $1 WHERE id = $2")
        .bind(now)
        .bind(&matched_id)
        .execute(&mut *tx)
        .await?;
    sqlx::query(
        "INSERT INTO portal_auth.login_token \
         (id, identity_id, kind, code_hash, action_label, issued_at, expires_at) \
         VALUES ($1, $2, 'step_up_grant', $3, $4, $5, $6)",
    )
    .bind(hex_id("LTK"))
    .bind(&sess)
    .bind(&grant_hash)
    .bind(action_label)
    .bind(now)
    .bind(grant_expires)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;

    Ok(StepUpVerify::Token(StepUpToken {
        token: grant,
        expires_at: grant_expires,
        action_label: action_label.to_string(),
    }))
}

/// Validate + atomically consume a one-shot step-up grant at the moment of a
/// sensitive write. Port of `consume_step_up_token`.
pub async fn consume_step_up_token(
    pool: &PgPool,
    session_id: &str,
    token: &str,
    action_label: &str,
) -> Result<bool, sqlx::Error> {
    let now = bss_clock::now();
    let Some(sess) = session_identity_if_active(pool, session_id).await? else {
        return Ok(false);
    };

    let rows = sqlx::query(
        "SELECT id, code_hash, expires_at FROM portal_auth.login_token \
         WHERE identity_id = $1 AND kind = 'step_up_grant' AND action_label = $2 \
           AND consumed_at IS NULL",
    )
    .bind(&sess)
    .bind(action_label)
    .fetch_all(pool)
    .await?;

    for row in &rows {
        let expires: DateTime<Utc> = row.get("expires_at");
        if expires <= now {
            continue;
        }
        let hash: String = row.get("code_hash");
        if verify_token(token, &hash, None) {
            let id: String = row.get("id");
            sqlx::query("UPDATE portal_auth.login_token SET consumed_at = $1 WHERE id = $2")
                .bind(now)
                .bind(&id)
                .execute(pool)
                .await?;
            return Ok(true);
        }
    }
    Ok(false)
}

/// Return the identity id for an active (non-revoked) session, else `None`.
async fn session_identity_if_active(
    pool: &PgPool,
    session_id: &str,
) -> Result<Option<String>, sqlx::Error> {
    let row = sqlx::query("SELECT identity_id, revoked_at FROM portal_auth.session WHERE id = $1")
        .bind(session_id)
        .fetch_optional(pool)
        .await?;
    match row {
        Some(r) if r.get::<Option<DateTime<Utc>>, _>("revoked_at").is_none() => {
            Ok(Some(r.get("identity_id")))
        }
        _ => Ok(None),
    }
}
