//! Deterministic regression for the open-order expiry sweep (v-reservation phase 4).
//!
//! `#[ignore]` — needs the live Postgres (`BSS_DB_URL`), like the other crm live
//! tests. **No clock manipulation and no sleeps:** one order is seeded with a PAST
//! `reserved_until` (must be swept → `expired` + number released) and another with
//! a FUTURE one (must be left untouched), then `worker::sweep_expired` runs once.
//! This pins both directions — the sweep releases lapsed holds and never
//! over-releases a live one.
//!
//! ```bash
//! set -a; source .env; set +a
//! cargo test -p crm --test open_order_sweep -- --ignored --nocapture
//! ```
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{SubscriptionClient, TokenAuthProvider};
use chrono::Duration;
use crm::config::{normalize_db_url, Settings};
use crm::state::AppState;
use sqlx::{PgPool, Row};

const TENANT: &str = "DEFAULT";
const OO_PAST: &str = "OO-TESTSWEEP-PAST";
const OO_FUTURE: &str = "OO-TESTSWEEP-FUTURE";

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

async fn build_state(pool: PgPool) -> AppState {
    let sub = SubscriptionClient::new(
        env("BSS_SUBSCRIPTION_URL").unwrap_or_else(|| "http://localhost:8006".into()),
        Arc::new(TokenAuthProvider::new(env("BSS_API_TOKEN").expect("BSS_API_TOKEN")).unwrap()),
    )
    .unwrap();
    AppState {
        pool,
        subscription: sub,
        loyalty: None,
        settings: Settings::from_env(),
    }
}

async fn scalar(pool: &PgPool, sql: &str, id: &str) -> Option<String> {
    sqlx::query(sql)
        .bind(id)
        .fetch_optional(pool)
        .await
        .unwrap()
        .map(|r| r.get::<String, _>(0))
}

/// `(status, reserved_for)` for a pool number.
async fn msisdn_state(pool: &PgPool, msisdn: &str) -> (String, Option<String>) {
    let r = sqlx::query("SELECT status, reserved_for FROM inventory.msisdn_pool WHERE msisdn=$1")
        .bind(msisdn)
        .fetch_one(pool)
        .await
        .unwrap();
    (r.get("status"), r.get("reserved_for"))
}

async fn cleanup(pool: &PgPool, nums: &[String]) {
    sqlx::query("DELETE FROM inventory.open_order WHERE id = ANY($1)")
        .bind(vec![OO_PAST.to_string(), OO_FUTURE.to_string()])
        .execute(pool)
        .await
        .unwrap();
    sqlx::query(
        "UPDATE inventory.msisdn_pool SET status='available', reserved_at=NULL, \
         reserved_until=NULL, reserved_for=NULL WHERE msisdn = ANY($1)",
    )
    .bind(nums)
    .execute(pool)
    .await
    .unwrap();
}

#[tokio::test]
#[ignore = "hits the live Postgres; run with --ignored"]
async fn sweep_expires_past_holds_and_spares_future() {
    let db = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    let pool = bss_db::connect(&db).await.expect("connect live Postgres");

    // Two available numbers to hold.
    let nums: Vec<String> = sqlx::query(
        "SELECT msisdn FROM inventory.msisdn_pool WHERE status='available' AND tenant_id=$1 \
         ORDER BY msisdn LIMIT 2",
    )
    .bind(TENANT)
    .fetch_all(&pool)
    .await
    .unwrap()
    .iter()
    .map(|r| r.get::<String, _>("msisdn"))
    .collect();
    assert_eq!(nums.len(), 2, "need 2 available msisdns to run the test");
    let (m_past, m_future) = (nums[0].clone(), nums[1].clone());

    cleanup(&pool, &nums).await; // clear any leftovers from a prior run

    let now = bss_clock::now();
    let past = now - Duration::hours(1);
    let future = now + Duration::hours(1);

    // Seed: an expired order + a still-live one, each holding a number.
    {
        let mut tx = pool.begin().await.unwrap();
        crm::repo::insert_open_order(
            &mut tx,
            OO_PAST,
            "sweep-past@test",
            "PLAN_M",
            &m_past,
            past,
            now,
            TENANT,
        )
        .await
        .unwrap();
        crm::repo::hold_msisdn(&mut tx, &m_past, OO_PAST, past, now, TENANT)
            .await
            .unwrap();
        crm::repo::insert_open_order(
            &mut tx,
            OO_FUTURE,
            "sweep-future@test",
            "PLAN_M",
            &m_future,
            future,
            now,
            TENANT,
        )
        .await
        .unwrap();
        crm::repo::hold_msisdn(&mut tx, &m_future, OO_FUTURE, future, now, TENANT)
            .await
            .unwrap();
        tx.commit().await.unwrap();
    }

    // One sweep pass.
    let state = build_state(pool.clone()).await;
    crm::worker::sweep_expired(&state).await.expect("sweep");

    // Expired → order 'expired', number released.
    assert_eq!(
        scalar(
            &pool,
            "SELECT status FROM inventory.open_order WHERE id=$1",
            OO_PAST
        )
        .await,
        Some("expired".to_string())
    );
    let (past_status, past_owner) = msisdn_state(&pool, &m_past).await;
    assert_eq!(past_status, "available", "lapsed hold must be released");
    assert!(past_owner.is_none(), "reserved_for must be cleared");

    // Future → untouched.
    assert_eq!(
        scalar(
            &pool,
            "SELECT status FROM inventory.open_order WHERE id=$1",
            OO_FUTURE
        )
        .await,
        Some("open".to_string())
    );
    let (future_status, future_owner) = msisdn_state(&pool, &m_future).await;
    assert_eq!(future_status, "reserved", "live hold must not be released");
    assert_eq!(future_owner.as_deref(), Some(OO_FUTURE));

    cleanup(&pool, &nums).await;
}
