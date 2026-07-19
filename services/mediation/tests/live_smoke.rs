//! Live smoke — the Phase-2 analogue of the Phase-0/1 conformance harness, for
//! mediation. Proves the Rust mediation service against the **live** stack (the
//! same Postgres + Subscription the Python services use). `#[ignore]` so CI (no
//! infra) skips it; run manually with the stack up:
//!
//! ```bash
//! set -a; source ../../.env; set +a          # from rust/services/mediation
//! BSS_SUBSCRIPTION_URL=http://localhost:8006 \
//!   cargo test -p mediation --test live_smoke -- --ignored --nocapture
//! ```
//!
//! Mediation has no consumer — it's the HTTP producer of `usage.recorded`. The
//! meaningful *inert* proof is the block-at-edge **rejection** path: a usage POST
//! for a bogus MSISDN reaches live Subscription (a real 404), is rejected 422 with
//! `subscription_must_exist`, leaves **no** `usage_event` row, and writes a single
//! `usage.rejected` audit row. Everything is scoped by a unique CDR ref and
//! cleaned up — no seeded balance is ever touched.
//!
//! What it checks:
//! 1. `SubscriptionClient::get_by_msisdn` against live Subscription → `NotFound`
//!    for a bogus MSISDN (catches R1 drift on the enrichment contract);
//! 2. the full HTTP stack (token gate + context + routes + error mapping):
//!    health / 401 / pre-enrich policy 422 / rejection 422 + `usage.rejected`
//!    audit row / no `usage_event` row / GET 404 / audit read — cleaned up.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{ClientError, SubscriptionClient, TokenAuthProvider};
use bss_middleware::TokenMap;
use mediation::config::{normalize_db_url, Settings};
use mediation::state::AppState;
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn subscription_url() -> String {
    env("BSS_SUBSCRIPTION_URL").unwrap_or_else(|| "http://localhost:8006".to_string())
}

fn token() -> String {
    env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set for the live smoke")
}

async fn live_pool() -> sqlx::PgPool {
    let url =
        normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set for the live smoke"));
    bss_db::connect(&url).await.expect("connect live Postgres")
}

fn live_subscription() -> SubscriptionClient {
    let auth = Arc::new(TokenAuthProvider::new(token()).expect("token"));
    SubscriptionClient::new(subscription_url(), auth).expect("subscription client")
}

/// A MSISDN outside the seeded 9000xxxx pool — guaranteed to have no subscription.
const BOGUS_MSISDN: &str = "80000001";

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn subscription_client_notfound_on_bogus_msisdn() {
    let sub = live_subscription();
    let err = sub
        .get_by_msisdn(BOGUS_MSISDN)
        .await
        .expect_err("bogus MSISDN must not resolve to a subscription");
    assert!(
        matches!(err, ClientError::NotFound(_)),
        "expected NotFound from live Subscription, got {err:?}"
    );
    println!("[ok] SubscriptionClient.get_by_msisdn → NotFound against live Subscription");
}

async fn spawn_app() -> (String, sqlx::PgPool) {
    let pool = live_pool().await;
    let state = AppState {
        pool: pool.clone(),
        subscription: live_subscription(),
        settings: Settings::from_env(),
        mq: None, // inert — audit rows staged with published_to_mq=false, no broker needed
    };
    let token_map = Arc::new(TokenMap::single(&token()));
    let app = mediation::create_app(state, token_map);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    (format!("http://{addr}"), pool)
}

const USAGE_PATH: &str = "/tmf-api/usageManagement/v4/usage";

fn usage_body(msisdn: &str, event_type: &str, quantity: i64, cdr_ref: &str) -> Value {
    json!({
        "msisdn": msisdn,
        "eventType": event_type,
        "eventTime": chrono::Utc::now().to_rfc3339(),
        "quantity": quantity,
        "unit": "mb",
        "source": "live-smoke",
        "rawCdrRef": cdr_ref,
    })
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn http_surface_end_to_end() {
    let (base, pool) = spawn_app().await;
    let http = reqwest::Client::new();
    let tok = token();
    let cdr_ref = format!("CDR-SMOKE-{}", chrono::Utc::now().timestamp_millis());

    // /health — exempt, no token.
    let r = http.get(format!("{base}/health")).send().await.unwrap();
    assert_eq!(r.status(), 200);
    assert_eq!(r.json::<Value>().await.unwrap()["service"], "mediation");

    // POST /usage without token → 401 (only /health* is exempt).
    let r = http
        .post(format!("{base}{USAGE_PATH}"))
        .json(&usage_body(BOGUS_MSISDN, "data", 100, &cdr_ref))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 401, "no-token usage POST must be 401");

    // Pre-enrichment policy: bad event type → 422 usage.record.valid_event_type,
    // fully inert (raises before any enrichment / rejection audit).
    let r = http
        .post(format!("{base}{USAGE_PATH}"))
        .header("X-BSS-API-Token", &tok)
        .json(&usage_body(BOGUS_MSISDN, "video", 1, "CDR-SMOKE-NOPE"))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 422);
    assert_eq!(
        r.json::<Value>().await.unwrap()["reason"],
        "usage.record.valid_event_type"
    );

    // Rejection path: bogus MSISDN → live Subscription 404 → 422
    // subscription_must_exist, with a usage.rejected audit row and NO usage_event.
    let r = http
        .post(format!("{base}{USAGE_PATH}"))
        .header("X-BSS-API-Token", &tok)
        .json(&usage_body(BOGUS_MSISDN, "data", 100, &cdr_ref))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 422);
    assert_eq!(
        r.json::<Value>().await.unwrap()["reason"],
        "usage.record.subscription_must_exist"
    );

    // No usage_event row was written for the rejected CDR.
    let usage_rows: i64 =
        sqlx::query_scalar("SELECT count(*) FROM mediation.usage_event WHERE raw_cdr_ref = $1")
            .bind(&cdr_ref)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        usage_rows, 0,
        "a rejected CDR must leave no usage_event row"
    );

    // Exactly one usage.rejected audit row, scoped to this run's CDR ref.
    let r = http
        .get(format!(
            "{base}/audit-api/v1/events?eventType=usage.rejected&limit=100"
        ))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    let events = r.json::<Value>().await.unwrap();
    let mine: Vec<&Value> = events["events"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["payload"]["rawCdrRef"] == cdr_ref)
        .collect();
    assert_eq!(
        mine.len(),
        1,
        "expected exactly one usage.rejected for {cdr_ref}"
    );
    assert_eq!(
        mine[0]["payload"]["reason"],
        "usage.record.subscription_must_exist"
    );
    assert_eq!(mine[0]["publishedToMq"], false); // mq=None in the smoke

    // GET a non-existent event → 404.
    let r = http
        .get(format!("{base}{USAGE_PATH}/UE-999999"))
        .header("X-BSS-API-Token", &tok)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 404);

    // Clean up the inert rejection audit row.
    let deleted =
        sqlx::query("DELETE FROM audit.domain_event WHERE event_type = 'usage.rejected' AND payload->>'rawCdrRef' = $1")
            .bind(&cdr_ref)
            .execute(&pool)
            .await
            .unwrap()
            .rows_affected();
    assert_eq!(deleted, 1);
    println!("[ok] HTTP surface (health/401/policy-422/rejection-422/no-row/404/audit) — cleaned up {cdr_ref}");
}
