//! Live smoke + golden diff — the Phase-4 analogue of the conformance harness.
//! `#[ignore]` so CI skips it; run with the stack up (Python subscription on :8006):
//!
//! ```bash
//! set -a; source ../../../.env; set +a          # repo-root .env, from rust/
//! export BSS_SUBSCRIPTION_URL=http://localhost:8006
//! cargo test -p subscription --test live_smoke -- --ignored --nocapture
//! ```
//!
//! The core check is a **golden diff**: boot the Rust subscription surface
//! in-process against the same live Postgres, then assert each read endpoint's
//! JSON is byte-equal (semantic `Value ==`, order-sensitive for arrays — so the
//! balances order, the `amount` strings, `effectiveAmount`, and `Z` datetime
//! rendering are all covered) to the live Python oracle's. Everything read-only;
//! the sample ids are discovered from the live DB so the test needs no fixed seed.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use bss_clients::{CatalogClient, CrmClient, InventoryClient, PaymentClient, TokenAuthProvider};
use bss_middleware::TokenMap;
use serde_json::Value;
use sqlx::Row;
use subscription::config::{normalize_db_url, Settings};
use subscription::state::AppState;

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn token() -> String {
    env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set")
}

fn oracle_url() -> String {
    env("BSS_SUBSCRIPTION_URL").unwrap_or_else(|| "http://localhost:8006".to_string())
}

struct Sample {
    id: String,
    customer_id: String,
    msisdn: String,
}

async fn discover(pool: &sqlx::PgPool) -> Sample {
    let r = sqlx::query(
        "SELECT id, customer_id, msisdn FROM subscription.subscription \
         ORDER BY created_at DESC LIMIT 1",
    )
    .fetch_one(pool)
    .await
    .expect("need at least one subscription seeded");
    Sample {
        id: r.get("id"),
        customer_id: r.get("customer_id"),
        msisdn: r.get("msisdn"),
    }
}

fn auth() -> Arc<TokenAuthProvider> {
    Arc::new(TokenAuthProvider::new(token()).unwrap())
}

async fn spawn_app(pool: sqlx::PgPool) -> String {
    let crm = CrmClient::new(
        env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".into()),
        auth(),
    )
    .unwrap();
    let payment = PaymentClient::new(
        env("BSS_PAYMENT_URL").unwrap_or_else(|| "http://localhost:8003".into()),
        auth(),
    )
    .unwrap();
    let catalog = CatalogClient::new(
        env("BSS_CATALOG_URL").unwrap_or_else(|| "http://localhost:8001".into()),
        auth(),
    )
    .unwrap();
    let inventory = InventoryClient::new(
        env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".into()),
        auth(),
    )
    .unwrap();
    let state = AppState {
        pool,
        crm,
        payment,
        catalog,
        inventory,
        settings: Settings::from_env(),
    };
    let token_map = Arc::new(TokenMap::single(&token()));
    let app = subscription::create_app(state, token_map);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    format!("http://{addr}")
}

async fn get_json(http: &reqwest::Client, base: &str, path: &str) -> (u16, Value) {
    let r = http
        .get(format!("{base}{path}"))
        .header("X-BSS-API-Token", token())
        .timeout(Duration::from_secs(10))
        .send()
        .await
        .unwrap();
    let status = r.status().as_u16();
    let body = r.json::<Value>().await.unwrap_or(Value::Null);
    (status, body)
}

async fn golden(http: &reqwest::Client, rust: &str, oracle: &str, path: &str) {
    let (rs, rb) = get_json(http, rust, path).await;
    let (ps, pb) = get_json(http, oracle, path).await;
    assert_eq!(rs, ps, "status mismatch on {path}");
    assert_eq!(
        rb, pb,
        "golden diff on {path}:\n  rust  = {rb}\n  python= {pb}"
    );
}

const API: &str = "/subscription-api/v1/subscription";

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn golden_diff_vs_python_oracle() {
    let db = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    let pool = bss_db::connect(&db).await.expect("connect live Postgres");
    let s = discover(&pool).await;
    let rust = spawn_app(pool).await;
    let oracle = oracle_url();
    let http = reqwest::Client::new();

    // Single subscription (covers balances order, price strings, effectiveAmount,
    // discount fields, Z datetimes, atType).
    golden(&http, &rust, &oracle, &format!("{API}/{}", s.id)).await;
    // List for the customer.
    golden(
        &http,
        &rust,
        &oracle,
        &format!("{API}?customerId={}", s.customer_id),
    )
    .await;
    // By-msisdn enrichment lookup.
    golden(
        &http,
        &rust,
        &oracle,
        &format!("{API}/by-msisdn/{}", s.msisdn),
    )
    .await;
    // Balances endpoint.
    golden(&http, &rust, &oracle, &format!("{API}/{}/balance", s.id)).await;

    // 404 envelopes.
    golden(&http, &rust, &oracle, &format!("{API}/SUB-000000")).await;
    golden(&http, &rust, &oracle, &format!("{API}/by-msisdn/00000000")).await;
    golden(&http, &rust, &oracle, &format!("{API}/SUB-000000/balance")).await;

    println!("subscription golden diff: all endpoints byte-identical to the oracle");
}

/// The token perimeter: `/health` is exempt (200 without a token); a real API
/// route 401s without one; the live token passes.
#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn token_perimeter_matches_oracle() {
    let db = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    let pool = bss_db::connect(&db).await.expect("connect live Postgres");
    let rust = spawn_app(pool).await;
    let http = reqwest::Client::new();

    let health = http.get(format!("{rust}/health")).send().await.unwrap();
    assert_eq!(health.status().as_u16(), 200, "/health must be exempt");

    let no_token = http
        .get(format!("{rust}{API}?customerId=CUST-x"))
        .send()
        .await
        .unwrap();
    assert_eq!(
        no_token.status().as_u16(),
        401,
        "route must require a token"
    );

    let with_token = http
        .get(format!("{rust}{API}?customerId=CUST-x"))
        .header("X-BSS-API-Token", token())
        .send()
        .await
        .unwrap();
    assert_eq!(with_token.status().as_u16(), 200, "live token must pass");
}
