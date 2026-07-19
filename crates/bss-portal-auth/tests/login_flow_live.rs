//! Live end-to-end login round-trip against the real `portal_auth` schema.
//! `#[ignore]` — needs `BSS_DB_URL` + `BSS_PORTAL_TOKEN_PEPPER` + tech-vm.
//!
//! ```bash
//! set -a; source ../../../.env; set +a
//! cargo test -p bss-portal-auth --test login_flow_live -- --ignored --nocapture
//! ```
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_portal_auth::{
    current_session, revoke_session, start_email_login, verify_email_login, NoopEmailAdapter,
    VerifyOutcome,
};

#[tokio::test]
#[ignore = "needs BSS_DB_URL + BSS_PORTAL_TOKEN_PEPPER + live portal_auth schema"]
async fn full_login_round_trip() {
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

    // Unique email so rate limits + prior rows don't collide.
    let email = format!("rusttest-{}@example.test", uuid_like());
    let adapter = NoopEmailAdapter::new();

    // 1. start_email_login writes identity + 2 tokens, "sends" the OTP.
    start_email_login(&pool, &email, Some("10.0.0.9"), Some("rust-test"), &adapter)
        .await
        .expect("start_email_login");
    let (otp, _magic) = adapter.last_login_codes(&email).expect("otp captured");
    assert_eq!(otp.len(), 6);

    // 2. wrong code → Failed(wrong_code).
    match verify_email_login(&pool, &email, "000000", None, None)
        .await
        .unwrap()
    {
        VerifyOutcome::Failed(f) => assert_eq!(f.reason, "wrong_code"),
        VerifyOutcome::Session(_) => panic!("wrong code should not mint a session"),
    }

    // 3. correct OTP → Session.
    let sess = match verify_email_login(&pool, &email, &otp, None, None)
        .await
        .unwrap()
    {
        VerifyOutcome::Session(s) => s,
        VerifyOutcome::Failed(f) => panic!("correct OTP failed: {}", f.reason),
    };

    // 4. the session cookie resolves via current_session.
    let resolved = current_session(&pool, &sess.id).await.unwrap();
    let (rs, ident) = resolved.expect("session resolves");
    assert_eq!(rs.id, sess.id);
    assert_eq!(ident.email, email);
    assert!(ident.email_verified_at.is_some());

    // 5. re-using the consumed OTP → wrong_code: the OTP token is consumed, but
    // the sibling magic_link token is still active+unexpired, so the verify sees
    // an active token and reports wrong_code (not no_active_token). Matches the
    // oracle (both tokens are minted per login; only the matched one consumes).
    match verify_email_login(&pool, &email, &otp, None, None)
        .await
        .unwrap()
    {
        VerifyOutcome::Failed(f) => assert_eq!(f.reason, "wrong_code"),
        VerifyOutcome::Session(_) => panic!("consumed OTP should not re-mint"),
    }

    // Cleanup: revoke the session (identity/token rows are harmless test data).
    revoke_session(&pool, &sess.id).await.unwrap();
    println!("login round-trip OK for {email}");
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
