//! Live smoke for the DB session layer against the real `portal_auth` schema.
//! `#[ignore]` — needs `BSS_DB_URL` + the tech-vm Postgres.
//!
//! ```bash
//! set -a; source ../../../.env; set +a
//! cargo test -p bss-portal-auth --test service_live -- --ignored --nocapture
//! ```
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_portal_auth::{current_session, revoke_session, rotate_if_due};

fn db_url() -> Option<String> {
    std::env::var("BSS_DB_URL").ok().filter(|v| !v.is_empty())
}

#[tokio::test]
#[ignore = "needs BSS_DB_URL + the live portal_auth schema"]
async fn session_queries_are_schema_valid() {
    let Some(url) = db_url() else {
        eprintln!("BSS_DB_URL unset — skipping");
        return;
    };
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(&url)
        .await
        .expect("connect portal_auth");

    // A bogus cookie must resolve to None without a SQL error — proving the
    // column list + schema qualifier match the live `portal_auth.session` /
    // `portal_auth.identity` tables.
    let got = current_session(&pool, "SES-does-not-exist")
        .await
        .expect("current_session query is schema-valid");
    assert!(got.is_none());

    // rotate + revoke on a missing id are no-ops (also schema-valid).
    assert!(rotate_if_due(&pool, "SES-does-not-exist")
        .await
        .expect("rotate query valid")
        .is_none());
    revoke_session(&pool, "SES-does-not-exist")
        .await
        .expect("revoke query valid");
}
