//! Email-change two-step flow with cross-schema atomic verification. Port of
//! `bss_portal_auth.email_change`.
//!
//! The CRM `contact_medium` update and the `portal_auth.identity.email` update
//! must commit together or roll back together — sequential commits leave CRM and
//! portal_auth mismatched with no easy recovery (V0_10_0.md). Both schemas share
//! one Postgres instance, so a single sqlx transaction spans them. Writing
//! directly to `crm.contact_medium` from here is the documented doctrine
//! exception (DECISIONS 2026-04-27, "v0.10 PR 8").

use chrono::{Duration, Utc};
use sqlx::{PgPool, Row};

use crate::email::EmailAdapter;
use crate::tokens::{generate_otp, hash_token, verify_token};

/// A pending row exists; the OTP is in transit to `new_email`.
#[derive(Debug, Clone)]
pub struct EmailChangeStarted {
    pub pending_id: String,
    pub new_email: String,
}

/// Cross-schema commit completed; both rows now reflect the new email.
#[derive(Debug, Clone)]
pub struct EmailChangeApplied {
    pub new_email: String,
}

/// Generic failure with a structured reason for the route to branch on:
/// `no_active_pending` | `wrong_code` | `expired` | `email_in_use`.
#[derive(Debug, Clone)]
pub struct EmailChangeFailed {
    pub reason: String,
}

/// Outcome of [`start_email_change`].
pub enum StartOutcome {
    Started(EmailChangeStarted),
    Failed(EmailChangeFailed),
}

/// Outcome of [`verify_email_change`].
pub enum VerifyChangeOutcome {
    Applied(EmailChangeApplied),
    Failed(EmailChangeFailed),
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
    format!("ECP-{hex}")
}

/// Begin an email-change flow: uniqueness check → void prior pending → insert a
/// fresh pending row + OTP → send the OTP to the *new* email. Port of
/// `start_email_change`.
pub async fn start_email_change(
    pool: &PgPool,
    identity_id: &str,
    new_email: &str,
    ip: Option<&str>,
    user_agent: Option<&str>,
    email_adapter: &dyn EmailAdapter,
) -> Result<StartOutcome, sqlx::Error> {
    let new_email = new_email.trim().to_lowercase();

    // Up-front uniqueness check against active email contact mediums.
    let existing = sqlx::query(
        "SELECT id FROM crm.contact_medium \
         WHERE medium_type = 'email' AND value = $1 AND valid_to IS NULL LIMIT 1",
    )
    .bind(&new_email)
    .fetch_optional(pool)
    .await?;
    if existing.is_some() {
        return Ok(StartOutcome::Failed(EmailChangeFailed {
            reason: "email_in_use".to_string(),
        }));
    }

    let now = Utc::now();
    let otp = generate_otp();
    #[allow(clippy::expect_used)]
    let code_hash = hash_token(&otp, None).expect("pepper validated at startup");
    let pid = pending_id();

    let mut tx = pool.begin().await?;
    // Void any prior pending row.
    sqlx::query(
        "UPDATE portal_auth.email_change_pending SET status = 'cancelled' \
         WHERE identity_id = $1 AND status = 'pending'",
    )
    .bind(identity_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "INSERT INTO portal_auth.email_change_pending \
         (id, identity_id, new_email, code_hash, issued_at, expires_at, status, ip, user_agent) \
         VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8)",
    )
    .bind(&pid)
    .bind(identity_id)
    .bind(&new_email)
    .bind(&code_hash)
    .bind(now)
    .bind(now + Duration::hours(24))
    .bind(ip)
    .bind(user_agent)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;

    // The OTP goes to the NEW email — the customer can't verify without it.
    email_adapter.send_step_up(&new_email, &otp, "email_change");

    Ok(StartOutcome::Started(EmailChangeStarted {
        pending_id: pid,
        new_email,
    }))
}

/// Verify the OTP and atomically commit the email change across CRM +
/// portal_auth. Port of `verify_email_change`. All four writes land in one
/// transaction (validate → CRM contact_medium → identity.email → mark consumed).
pub async fn verify_email_change(
    pool: &PgPool,
    identity_id: &str,
    code: &str,
) -> Result<VerifyChangeOutcome, sqlx::Error> {
    let mut tx = pool.begin().await?;

    // Step 1: validate the OTP against the active pending row.
    let pending = sqlx::query(
        "SELECT id, new_email, code_hash, expires_at FROM portal_auth.email_change_pending \
         WHERE identity_id = $1 AND status = 'pending'",
    )
    .bind(identity_id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(pending) = pending else {
        return Ok(fail("no_active_pending"));
    };
    let now = Utc::now();
    let expires: chrono::DateTime<Utc> = pending.get("expires_at");
    let pending_pk: String = pending.get("id");
    let new_email: String = pending.get("new_email");
    let code_hash: String = pending.get("code_hash");

    if expires <= now {
        sqlx::query("UPDATE portal_auth.email_change_pending SET status = 'expired' WHERE id = $1")
            .bind(&pending_pk)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        return Ok(fail("expired"));
    }
    if !verify_token(code.trim(), &code_hash, None) {
        return Ok(fail("wrong_code"));
    }

    // Step 2: resolve identity → customer → party's active email medium.
    let identity = sqlx::query("SELECT customer_id FROM portal_auth.identity WHERE id = $1")
        .bind(identity_id)
        .fetch_optional(&mut *tx)
        .await?;
    let customer_id: Option<String> = identity.and_then(|r| r.get("customer_id"));
    let Some(customer_id) = customer_id else {
        return Ok(fail("no_active_pending"));
    };
    let party = sqlx::query("SELECT party_id FROM crm.customer WHERE id = $1")
        .bind(&customer_id)
        .fetch_optional(&mut *tx)
        .await?;
    let Some(party) = party else {
        return Ok(fail("no_active_pending"));
    };
    let party_id: String = party.get("party_id");

    let cm = sqlx::query(
        "SELECT id FROM crm.contact_medium \
         WHERE party_id = $1 AND medium_type = 'email' AND valid_to IS NULL",
    )
    .bind(&party_id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(cm) = cm else {
        return Ok(fail("no_active_pending"));
    };
    let cm_id: String = cm.get("id");

    // Steps 2–4: CRM contact_medium.value, identity.email, pending consumed.
    sqlx::query("UPDATE crm.contact_medium SET value = $1 WHERE id = $2")
        .bind(&new_email)
        .bind(&cm_id)
        .execute(&mut *tx)
        .await?;
    sqlx::query("UPDATE portal_auth.identity SET email = $1 WHERE id = $2")
        .bind(&new_email)
        .bind(identity_id)
        .execute(&mut *tx)
        .await?;
    sqlx::query(
        "UPDATE portal_auth.email_change_pending \
         SET status = 'consumed', consumed_at = $1 WHERE id = $2",
    )
    .bind(now)
    .bind(&pending_pk)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;

    Ok(VerifyChangeOutcome::Applied(EmailChangeApplied {
        new_email,
    }))
}

/// Cancel the active pending row, if any. Returns `true` iff one was cancelled.
pub async fn cancel_pending_email_change(
    pool: &PgPool,
    identity_id: &str,
) -> Result<bool, sqlx::Error> {
    let res = sqlx::query(
        "UPDATE portal_auth.email_change_pending SET status = 'cancelled' \
         WHERE identity_id = $1 AND status = 'pending'",
    )
    .bind(identity_id)
    .execute(pool)
    .await?;
    Ok(res.rows_affected() > 0)
}

fn fail(reason: &str) -> VerifyChangeOutcome {
    VerifyChangeOutcome::Failed(EmailChangeFailed {
        reason: reason.to_string(),
    })
}
