//! Live round-trip for the two P6b-signup DB writes — `link_to_customer` and
//! `record_portal_action` — against the real `portal_auth` schema.
//! `#[ignore]` — needs `BSS_DB_URL` + tech-vm.
//!
//! ```bash
//! set -a; source ../../../.env; set +a
//! cargo test -p bss-portal-auth --test audit_link_live -- --ignored --nocapture
//! ```
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_portal_auth::{
    link_to_customer, record_portal_action, relink_orphaned_to_customer, LinkError,
    PortalActionRecord,
};
use sqlx::Row;

#[tokio::test]
#[ignore = "needs BSS_DB_URL + live portal_auth schema"]
async fn link_and_audit_round_trip() {
    let Some(url) = std::env::var("BSS_DB_URL").ok().filter(|v| !v.is_empty()) else {
        eprintln!("BSS_DB_URL unset — skipping");
        return;
    };
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(2)
        .connect(&url)
        .await
        .expect("connect");

    let suffix = uuid_like();
    let identity_id = format!("ID-RUSTTEST-{suffix}");
    let email = format!("rusttest-link-{suffix}@example.test");
    let cust_a = format!("CUST-RUSTTEST-{suffix}");
    let cust_b = format!("CUST-OTHER-{suffix}");

    // Seed a bare identity row (tenant_id via server default).
    sqlx::query(
        "INSERT INTO portal_auth.identity (id, email, status, created_at) \
         VALUES ($1, $2, 'verified', now())",
    )
    .bind(&identity_id)
    .bind(&email)
    .execute(&pool)
    .await
    .expect("seed identity");

    // 1. First link → Ok, and the identity row now points at cust_a.
    link_to_customer(&pool, &identity_id, &cust_a)
        .await
        .expect("first link");
    let linked: String = sqlx::query("SELECT customer_id FROM portal_auth.identity WHERE id = $1")
        .bind(&identity_id)
        .fetch_one(&pool)
        .await
        .unwrap()
        .get("customer_id");
    assert_eq!(linked, cust_a);

    // 2. Re-link same pair → idempotent Ok.
    link_to_customer(&pool, &identity_id, &cust_a)
        .await
        .expect("idempotent re-link");

    // 3. Link to a different customer → AlreadyLinked { existing: cust_a }.
    match link_to_customer(&pool, &identity_id, &cust_b).await {
        Err(LinkError::AlreadyLinked { existing }) => assert_eq!(existing, cust_a),
        other => panic!("expected AlreadyLinked, got {other:?}"),
    }

    // 4. Unknown identity → UnknownIdentity.
    match link_to_customer(&pool, "ID-NONEXISTENT-xyz", &cust_a).await {
        Err(LinkError::UnknownIdentity) => {}
        other => panic!("expected UnknownIdentity, got {other:?}"),
    }

    // 5. relink_orphaned_to_customer: the CAS must miss unless the caller names
    //    the id actually on the row. This is the takeover guard — an attacker who
    //    can pick `new_customer_id` still can't move a link they can't name.
    let cust_c = format!("CUST-THIRD-{suffix}");
    match relink_orphaned_to_customer(&pool, &identity_id, &cust_b, &cust_c).await {
        Err(LinkError::AlreadyLinked { existing }) => assert_eq!(existing, cust_a),
        other => panic!("expected AlreadyLinked on CAS miss, got {other:?}"),
    }
    let still: String = sqlx::query("SELECT customer_id FROM portal_auth.identity WHERE id = $1")
        .bind(&identity_id)
        .fetch_one(&pool)
        .await
        .unwrap()
        .get("customer_id");
    assert_eq!(still, cust_a, "CAS miss must not move the link");

    // 6. Naming the real (ghost) id repairs the link.
    relink_orphaned_to_customer(&pool, &identity_id, &cust_a, &cust_c)
        .await
        .expect("relink on CAS hit");
    let repaired: String =
        sqlx::query("SELECT customer_id FROM portal_auth.identity WHERE id = $1")
            .bind(&identity_id)
            .fetch_one(&pool)
            .await
            .unwrap()
            .get("customer_id");
    assert_eq!(repaired, cust_c);

    // 7. Unknown identity → UnknownIdentity, not a silent no-op.
    match relink_orphaned_to_customer(&pool, "ID-NONEXISTENT-xyz", &cust_a, &cust_c).await {
        Err(LinkError::UnknownIdentity) => {}
        other => panic!("expected UnknownIdentity, got {other:?}"),
    }

    // 8. record_portal_action writes one row with the resolved fields.
    let rec = PortalActionRecord {
        customer_id: Some(&cust_a),
        identity_id: Some(&identity_id),
        action: "signup_create_customer",
        route: "/signup",
        method: "POST",
        success: true,
        error_rule: None,
        step_up_consumed: false,
        ip: None,
        user_agent: Some("rust-test"),
    };
    record_portal_action(&pool, &rec)
        .await
        .expect("audit write");
    let n: i64 = sqlx::query(
        "SELECT count(*) AS c FROM portal_auth.portal_action \
         WHERE identity_id = $1 AND action = 'signup_create_customer'",
    )
    .bind(&identity_id)
    .fetch_one(&pool)
    .await
    .unwrap()
    .get("c");
    assert_eq!(n, 1);

    // Cleanup the rows this test created.
    sqlx::query("DELETE FROM portal_auth.portal_action WHERE identity_id = $1")
        .bind(&identity_id)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("DELETE FROM portal_auth.identity WHERE id = $1")
        .bind(&identity_id)
        .execute(&pool)
        .await
        .unwrap();

    println!("link + audit round-trip OK for {identity_id}");
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
