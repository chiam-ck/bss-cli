//! Live smoke — the Phase-1 analogue of the Phase-0 conformance harness.
//!
//! Proves the Rust rating service against the **live** stack (the same Postgres +
//! Catalog the Python services use). `#[ignore]` so CI (which has no infra) skips
//! it; run manually with the stack up:
//!
//! ```bash
//! set -a; source ../../.env; set +a        # from rust/services/rating
//! BSS_CATALOG_URL=http://localhost:8001 \
//!   cargo test -p rating --test live_smoke -- --ignored --nocapture
//! ```
//!
//! What it checks (all inert / cleaned up — never mutates seeded balances):
//! 1. `CatalogClient` fetches the real PLAN_M tariff and `rate_usage` rates it;
//! 2. the full HTTP stack (token gate + context + routes + error mapping) over a
//!    real request: health/tariff/rate-test/401/422;
//! 3. the `/audit-api/v1/events` router reads live Postgres;
//! 4. the outbox INSERT path (`handle_usage_recorded` with `mq=None`) writes a
//!    `usage.rated` audit row for an inert aggregate, reads it back through the
//!    router, then deletes it.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use bss_clients::{CatalogClient, TokenAuthProvider};
use bss_events::MqChannel;
use bss_middleware::TokenMap;
use rating::config::normalize_db_url;
use rating::state::AppState;
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn catalog_url() -> String {
    env("BSS_CATALOG_URL").unwrap_or_else(|| "http://localhost:8001".to_string())
}

async fn live_pool() -> sqlx::PgPool {
    let url =
        normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set for the live smoke"));
    bss_db::connect(&url).await.expect("connect live Postgres")
}

fn token() -> String {
    env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set for the live smoke")
}

fn live_catalog() -> CatalogClient {
    let auth = Arc::new(TokenAuthProvider::new(token()).expect("token"));
    CatalogClient::new(catalog_url(), auth).expect("catalog client")
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn catalog_client_rates_real_plan_m() {
    let catalog = live_catalog();
    let tariff = catalog
        .get_offering("PLAN_M")
        .await
        .expect("live catalog get_offering PLAN_M");
    assert_eq!(tariff["id"], "PLAN_M");

    let usage = rating::domain::UsageInput {
        usage_event_id: "UE-SMOKE".into(),
        subscription_id: "SUB-SMOKE".into(),
        msisdn: "90000042".into(),
        event_type: "data".into(),
        quantity: 100,
        unit: "mb".into(),
    };
    let result = rating::domain::rate_usage(&usage, &tariff).expect("rate real tariff");
    assert_eq!(result.allowance_type, "data");
    assert_eq!(result.charge_amount, "0");
    assert_eq!(result.currency, "SGD");
    println!("[ok] CatalogClient + rate_usage against live PLAN_M");
}

async fn spawn_app() -> (String, sqlx::PgPool) {
    let pool = live_pool().await;
    let state = AppState {
        pool: pool.clone(),
        catalog: live_catalog(),
        settings: rating::config::Settings::from_env(),
        mq: None,
    };
    let token_map = Arc::new(TokenMap::single(&token()));
    let app = rating::create_app(state, token_map);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    (format!("http://{addr}"), pool)
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn http_surface_end_to_end() {
    let (base, _pool) = spawn_app().await;
    let http = reqwest::Client::new();
    let tok = token();

    // /health — exempt, no token.
    let r = http.get(format!("{base}/health")).send().await.unwrap();
    assert_eq!(r.status(), 200);
    assert_eq!(r.json::<Value>().await.unwrap()["service"], "rating");

    // /tariff without token → 401 (only /health is exempt).
    let r = http
        .get(format!("{base}/rating-api/v1/tariff/PLAN_M"))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 401, "no-token tariff must be 401");

    // /tariff with token → 200, passes the live catalog doc through.
    let r = http
        .get(format!("{base}/rating-api/v1/tariff/PLAN_M"))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    assert_eq!(r.json::<Value>().await.unwrap()["id"], "PLAN_M");

    // /rate-test happy path.
    let r = http
        .post(format!("{base}/rating-api/v1/rate-test"))
        .header("X-BSS-API-Token", &tok)
        .json(&json!({
            "subscriptionId": "SUB-0001", "msisdn": "90000042",
            "offeringId": "PLAN_M", "eventType": "data", "quantity": 100, "unit": "mb"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    let body: Value = r.json().await.unwrap();
    assert_eq!(body["allowanceType"], "data");
    assert_eq!(body["consumedQuantity"], 100);
    assert_eq!(body["chargeAmount"], "0");
    assert_eq!(body["currency"], "SGD");

    // /rate-test unknown event type → 422 RATING_ERROR (middleware error shape).
    let r = http
        .post(format!("{base}/rating-api/v1/rate-test"))
        .header("X-BSS-API-Token", &tok)
        .json(&json!({
            "subscriptionId": "SUB-0001", "msisdn": "90000042",
            "offeringId": "PLAN_M", "eventType": "video", "quantity": 1, "unit": "mb"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 422);
    assert_eq!(r.json::<Value>().await.unwrap()["code"], "RATING_ERROR");

    // /audit-api/v1/events reads live Postgres.
    let r = http
        .get(format!("{base}/audit-api/v1/events?limit=1"))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    assert!(r.json::<Value>().await.unwrap().get("events").is_some());

    println!("[ok] HTTP surface (health/tariff/rate-test/401/422/audit) against live infra");
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn outbox_insert_and_audit_read_inert() {
    let (base, pool) = spawn_app().await;
    let catalog = live_catalog();
    let http = reqwest::Client::new();
    let tok = token();

    let agg = format!("RATINGSMOKE-{}", chrono::Utc::now().timestamp_millis());
    let body = json!({
        "usageEventId": agg,
        "subscriptionId": "SUB-SMOKE",
        "msisdn": "90000042",
        "eventType": "data",
        "quantity": 100,
        "unit": "mb",
        "offeringId": "PLAN_M",
    });

    // mq=None → stage the audit row without publishing (published_to_mq=false).
    rating::consumer::handle_usage_recorded(&body, &catalog, &pool, None)
        .await
        .expect("handle_usage_recorded (inert)");

    // Read it back through the audit router.
    let r = http
        .get(format!(
            "{base}/audit-api/v1/events?aggregateId={agg}&eventType=usage.rated"
        ))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    let events = r.json::<Value>().await.unwrap();
    assert_eq!(
        events["count"], 1,
        "expected exactly one staged usage.rated"
    );
    let ev = &events["events"][0];
    assert_eq!(ev["eventType"], "usage.rated");
    assert_eq!(ev["aggregateType"], "usage");
    assert_eq!(ev["payload"]["allowanceType"], "data");
    assert_eq!(ev["payload"]["consumedQuantity"], 100);
    assert_eq!(ev["publishedToMq"], false);
    // Consumer has no request context → default actor/identity (Python parity).
    assert_eq!(ev["actor"], "system");
    assert_eq!(ev["serviceIdentity"], "default");

    // Clean up the inert row.
    let deleted = sqlx::query("DELETE FROM audit.domain_event WHERE aggregate_id = $1")
        .bind(&agg)
        .execute(&pool)
        .await
        .unwrap()
        .rows_affected();
    assert_eq!(deleted, 1);
    println!("[ok] outbox INSERT + audit read-back (inert, cleaned up: {agg})");
}

#[tokio::test]
#[ignore = "container cutover: run ONLY with the Python `rating` container stopped"]
async fn consumer_cutover_end_to_end() {
    // The real Phase-1 exit shape: the Rust rating service, as the sole consumer
    // of `rating.usage.recorded` on the LIVE broker, turns a `usage.recorded`
    // into a `usage.rated` (audit row + published to MQ) via the live Catalog.
    //
    // Prereq (the runner script enforces it): `docker stop bss-cli-rating-1` so
    // this process — not the Python container — drains the shared durable queue.
    // subscriptionId is deliberately non-existent: subscription's handler catches
    // the not-found, logs, and acks (no park, no balance mutation).
    let pool = live_pool().await;
    let catalog = live_catalog();
    let mq_url = env("BSS_MQ_URL").expect("BSS_MQ_URL must be set");
    let mq = Arc::new(
        MqChannel::connect(&mq_url)
            .await
            .expect("connect live broker"),
    );

    // Start the real consumer loop.
    let consumer = tokio::spawn(rating::consumer::run(
        mq.clone(),
        pool.clone(),
        catalog.clone(),
    ));
    tokio::time::sleep(Duration::from_millis(700)).await; // let it bind + consume

    let agg = format!("RATINGCUT-{}", chrono::Utc::now().timestamp_millis());
    let recorded = json!({
        "usageEventId": agg,
        "subscriptionId": format!("SUB-CUTOVER-NONEXIST-{agg}"),
        "msisdn": "90000042",
        "eventType": "data",
        "quantity": 100,
        "unit": "mb",
        "offeringId": "PLAN_M",
    });
    mq.publish_json("usage.recorded", &recorded)
        .await
        .expect("publish usage.recorded");

    // Poll audit.domain_event for the Rust-written usage.rated.
    let mut found: Option<(bool, Value)> = None;
    for _ in 0..40 {
        let row = sqlx::query_as::<_, (bool, Value)>(
            "SELECT published_to_mq, payload FROM audit.domain_event \
             WHERE aggregate_id = $1 AND event_type = 'usage.rated'",
        )
        .bind(&agg)
        .fetch_optional(&pool)
        .await
        .expect("query audit");
        if let Some(r) = row {
            found = Some(r);
            break;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }

    // Clean up (delete the Rust-written row) regardless of assertion outcome.
    let _ = sqlx::query("DELETE FROM audit.domain_event WHERE aggregate_id = $1")
        .bind(&agg)
        .execute(&pool)
        .await;
    consumer.abort();

    let (published, payload) = found.expect("Rust rating did not emit usage.rated within 10s");
    assert!(
        published,
        "usage.rated should be published_to_mq=true (inline publish)"
    );
    assert_eq!(payload["allowanceType"], "data");
    assert_eq!(payload["consumedQuantity"], 100);
    assert_eq!(payload["chargeAmount"], "0");
    assert_eq!(payload["offeringId"], "PLAN_M");
    println!("[ok] consumer cutover: usage.recorded → Rust rating → usage.rated ({agg})");
}
