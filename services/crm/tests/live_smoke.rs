//! Live smoke + golden diff — the Phase-4 analogue of the conformance harness.
//! `#[ignore]` so CI skips it; run with the stack up (Python crm on :8002):
//!
//! ```bash
//! set -a; source ../../../.env; set +a          # repo-root .env, from rust/
//! export BSS_CRM_URL=http://localhost:8002
//! cargo test -p crm --test live_smoke -- --ignored --nocapture
//! ```
//!
//! Boots the Rust crm surface in-process against the same live Postgres and asserts
//! each read endpoint's JSON is byte-equal (`Value ==`, order-sensitive for arrays)
//! to the live Python oracle's — covering the TMF629/621/683 projections
//! (`@type`, `Z` datetimes, camelCase), the internal snake_case case/agent DTOs, and
//! the inventory pool shapes. Sample ids are discovered from the live DB.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use bss_clients::{SubscriptionClient, TokenAuthProvider};
use bss_middleware::TokenMap;
use crm::config::{normalize_db_url, Settings};
use crm::state::AppState;
use serde_json::Value;
use sqlx::Row;

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}
fn token() -> String {
    env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set")
}
fn oracle_url() -> String {
    env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string())
}

async fn one(pool: &sqlx::PgPool, sql: &str) -> Option<String> {
    sqlx::query(sql)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .map(|r| r.get::<String, _>(0))
}

async fn spawn_app(pool: sqlx::PgPool) -> String {
    let sub = SubscriptionClient::new(
        env("BSS_SUBSCRIPTION_URL").unwrap_or_else(|| "http://localhost:8006".into()),
        Arc::new(TokenAuthProvider::new(token()).unwrap()),
    )
    .unwrap();
    let state = AppState {
        pool,
        subscription: sub,
        loyalty: None,
        settings: Settings::from_env(),
    };
    let token_map = Arc::new(TokenMap::single(&token()));
    let app = crm::create_app(state, token_map);
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
    (
        r.status().as_u16(),
        r.json::<Value>().await.unwrap_or(Value::Null),
    )
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

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn golden_diff_vs_python_oracle() {
    let db = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    let pool = bss_db::connect(&db).await.expect("connect live Postgres");

    let cust = one(&pool, "SELECT id FROM crm.customer ORDER BY id LIMIT 1").await;
    let agent = one(&pool, "SELECT id FROM crm.agent ORDER BY id LIMIT 1").await;
    let msisdn = one(
        &pool,
        "SELECT msisdn FROM inventory.msisdn_pool ORDER BY msisdn LIMIT 1",
    )
    .await;
    let iccid = one(
        &pool,
        "SELECT iccid FROM inventory.esim_profile ORDER BY iccid LIMIT 1",
    )
    .await;
    let ticket = one(&pool, "SELECT id FROM crm.ticket ORDER BY id LIMIT 1").await;
    let case = one(&pool, "SELECT id FROM crm.\"case\" ORDER BY id LIMIT 1").await;
    let email = one(&pool, "SELECT value FROM crm.contact_medium WHERE medium_type='email' AND valid_to IS NULL LIMIT 1").await;

    let rust = spawn_app(pool).await;
    let oracle = oracle_url();
    let http = reqwest::Client::new();

    // Customer.
    if let Some(id) = &cust {
        golden(
            &http,
            &rust,
            &oracle,
            &format!("/tmf-api/customerManagement/v4/customer/{id}"),
        )
        .await;
    }
    golden(
        &http,
        &rust,
        &oracle,
        "/tmf-api/customerManagement/v4/customer?limit=5",
    )
    .await;
    golden(
        &http,
        &rust,
        &oracle,
        "/tmf-api/customerManagement/v4/customer/CUST-000000",
    )
    .await;
    if let Some(e) = &email {
        golden(
            &http,
            &rust,
            &oracle,
            &format!("/tmf-api/customerManagement/v4/customer/by-email?email={e}"),
        )
        .await;
    }

    // Inventory (the cross-service contract).
    if let Some(m) = &msisdn {
        golden(
            &http,
            &rust,
            &oracle,
            &format!("/inventory-api/v1/msisdn/{m}"),
        )
        .await;
    }
    golden(&http, &rust, &oracle, "/inventory-api/v1/msisdn?limit=5").await;
    golden(&http, &rust, &oracle, "/inventory-api/v1/msisdn/count").await;
    if let Some(i) = &iccid {
        golden(
            &http,
            &rust,
            &oracle,
            &format!("/inventory-api/v1/esim/{i}"),
        )
        .await;
        golden(
            &http,
            &rust,
            &oracle,
            &format!("/inventory-api/v1/esim/{i}/activation"),
        )
        .await;
    }
    golden(&http, &rust, &oracle, "/inventory-api/v1/esim?limit=5").await;

    // Ticket / case / agent / interaction / port.
    if let Some(t) = &ticket {
        golden(
            &http,
            &rust,
            &oracle,
            &format!("/tmf-api/troubleTicket/v4/troubleTicket/{t}"),
        )
        .await;
    }
    golden(
        &http,
        &rust,
        &oracle,
        "/tmf-api/troubleTicket/v4/troubleTicket?limit=5",
    )
    .await;
    if let Some(c) = &case {
        golden(&http, &rust, &oracle, &format!("/crm-api/v1/case/{c}")).await;
    }
    golden(&http, &rust, &oracle, "/crm-api/v1/case?limit=5").await;
    golden(&http, &rust, &oracle, "/crm-api/v1/agent?limit=5").await;
    if let Some(a) = &agent {
        golden(&http, &rust, &oracle, &format!("/crm-api/v1/agent/{a}")).await;
    }
    if let Some(id) = &cust {
        golden(
            &http,
            &rust,
            &oracle,
            &format!(
                "/tmf-api/customerInteractionManagement/v1/interaction?customerId={id}&limit=5"
            ),
        )
        .await;
        golden(
            &http,
            &rust,
            &oracle,
            &format!("/crm-api/v1/customer/{id}/kyc-status"),
        )
        .await;
    }
    golden(&http, &rust, &oracle, "/crm-api/v1/port-requests?limit=5").await;

    println!("crm golden diff: all read endpoints byte-identical to the oracle");
}

/// Token perimeter: `/health` exempt, a real route 401s without a token, passes with.
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
        .get(format!("{rust}/inventory-api/v1/msisdn/count"))
        .send()
        .await
        .unwrap();
    assert_eq!(
        no_token.status().as_u16(),
        401,
        "route must require a token"
    );

    let with_token = http
        .get(format!("{rust}/inventory-api/v1/msisdn/count"))
        .header("X-BSS-API-Token", token())
        .send()
        .await
        .unwrap();
    assert_eq!(with_token.status().as_u16(), 200, "live token must pass");
}
