//! Live round-trip for the step-up chain — start → verify → consume grant, plus
//! the pending-action stash/consume — against the real `portal_auth` schema.
//! `#[ignore]` — needs `BSS_DB_URL` + `BSS_PORTAL_TOKEN_PEPPER` + tech-vm.
//!
//! ```bash
//! set -a; source ../../../.env; set +a
//! cargo test -p bss-portal-auth --test stepup_live -- --ignored --nocapture
//! ```
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_portal_auth::{
    consume_pending_action, consume_step_up_token, start_step_up, stash_pending_action,
    verify_step_up, NoopEmailAdapter, StepUpVerify,
};

#[tokio::test]
#[ignore = "needs BSS_DB_URL + BSS_PORTAL_TOKEN_PEPPER + live portal_auth schema"]
async fn step_up_round_trip() {
    let Some(url) = std::env::var("BSS_DB_URL").ok().filter(|v| !v.is_empty()) else {
        eprintln!("BSS_DB_URL unset — skipping");
        return;
    };
    if std::env::var("BSS_PORTAL_TOKEN_PEPPER")
        .unwrap_or_default()
        .len()
        < 32
    {
        eprintln!("BSS_PORTAL_TOKEN_PEPPER unset/short — skipping");
        return;
    }
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(&url)
        .await
        .expect("connect");

    let suffix = uuid_like();
    let identity_id = format!("ID-STEPUP-{suffix}");
    let email = format!("stepup-{suffix}@example.test");
    let session_id = format!("SES-STEPUP-{suffix}");

    // Seed identity + an active session.
    sqlx::query(
        "INSERT INTO portal_auth.identity (id, email, status, created_at) \
         VALUES ($1, $2, 'verified', now())",
    )
    .bind(&identity_id)
    .bind(&email)
    .execute(&pool)
    .await
    .expect("seed identity");
    sqlx::query(
        "INSERT INTO portal_auth.session \
         (id, identity_id, issued_at, expires_at, last_seen_at) \
         VALUES ($1, $2, now(), now() + interval '1 hour', now())",
    )
    .bind(&session_id)
    .bind(&identity_id)
    .execute(&pool)
    .await
    .expect("seed session");

    let action = "vas_purchase";
    let adapter = NoopEmailAdapter::new();

    // 1. start_step_up → OTP captured.
    start_step_up(
        &pool,
        &session_id,
        action,
        None,
        Some("rust-test"),
        &adapter,
    )
    .await
    .expect("start_step_up");
    let otp = adapter.last_step_up_code(&email).expect("otp captured");
    assert_eq!(otp.len(), 6);

    // 2. wrong code → Failed(wrong_code).
    match verify_step_up(&pool, &session_id, "000000", action)
        .await
        .unwrap()
    {
        StepUpVerify::Failed(f) => assert_eq!(f.reason, "wrong_code"),
        StepUpVerify::Token(_) => panic!("wrong code minted a grant"),
    }

    // 3. correct OTP → grant token.
    let grant = match verify_step_up(&pool, &session_id, &otp, action)
        .await
        .unwrap()
    {
        StepUpVerify::Token(t) => t.token,
        StepUpVerify::Failed(f) => panic!("correct OTP failed: {}", f.reason),
    };

    // 4. wrong action_label → grant does not consume.
    assert!(
        !consume_step_up_token(&pool, &session_id, &grant, "email_change")
            .await
            .unwrap()
    );
    // 5. right label → consumes once.
    assert!(consume_step_up_token(&pool, &session_id, &grant, action)
        .await
        .unwrap());
    // 6. second consume → false (one-shot).
    assert!(!consume_step_up_token(&pool, &session_id, &grant, action)
        .await
        .unwrap());

    // 7. pending-action stash + consume round-trip (filters step_up_token).
    let payload = vec![
        ("subscription".to_string(), "SUB-9".to_string()),
        (
            "step_up_token".to_string(),
            "should-be-stripped".to_string(),
        ),
    ];
    stash_pending_action(
        &pool,
        &session_id,
        action,
        "/top-up?subscription=SUB-9",
        &payload,
        None,
    )
    .await
    .expect("stash");
    let pending = consume_pending_action(&pool, &session_id, action)
        .await
        .unwrap()
        .expect("pending row");
    assert_eq!(pending.target_url, "/top-up?subscription=SUB-9");
    assert!(pending
        .payload
        .iter()
        .any(|(k, v)| k == "subscription" && v == "SUB-9"));
    assert!(!pending.payload.iter().any(|(k, _)| k == "step_up_token"));
    // consumed → second consume is None.
    assert!(consume_pending_action(&pool, &session_id, action)
        .await
        .unwrap()
        .is_none());

    // Cleanup.
    for tbl in ["step_up_pending_action", "login_token"] {
        let sql = format!("DELETE FROM portal_auth.{tbl} WHERE identity_id = $1");
        // step_up_pending_action has session_id not identity_id.
        let sql = if tbl == "step_up_pending_action" {
            "DELETE FROM portal_auth.step_up_pending_action WHERE session_id = $1".to_string()
        } else {
            sql
        };
        let key = if tbl == "step_up_pending_action" {
            &session_id
        } else {
            &identity_id
        };
        sqlx::query(&sql).bind(key).execute(&pool).await.unwrap();
    }
    sqlx::query("DELETE FROM portal_auth.session WHERE id = $1")
        .bind(&session_id)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("DELETE FROM portal_auth.identity WHERE id = $1")
        .bind(&identity_id)
        .execute(&pool)
        .await
        .unwrap();

    println!("step-up round-trip OK for {identity_id}");
}

fn uuid_like() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    format!(
        "{:x}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    )
}
