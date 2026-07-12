//! Live smoke + golden diff — Phase-3 analogue of the conformance harness.
//! `#[ignore]` so CI skips it; run with the stack up (Python com on :8004):
//!
//! ```bash
//! set -a; source ../../.env; set +a          # from rust/services/com
//! export BSS_COM_URL=http://localhost:8004
//! cargo test -p com --test live_smoke -- --ignored --nocapture
//! ```
//!
//! The read surface (order get/list) is golden-diffed against the Python oracle
//! (`Value ==`, order-sensitive). The write/event pipeline (create → submit →
//! completed → subscription + promo consume) is exercised by the hero scenarios
//! after cutover. Everything here is read-only.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use bss_clients::{
    CatalogClient, CrmClient, PaymentClient, SomClient, SubscriptionClient, TokenAuthProvider,
};
use bss_middleware::TokenMap;
use com::config::{normalize_db_url, Settings};
use com::state::AppState;
use serde_json::Value;

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn token() -> String {
    env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set")
}

fn oracle_url() -> String {
    env("BSS_COM_URL").unwrap_or_else(|| "http://localhost:8004".to_string())
}

async fn spawn_app() -> String {
    let url = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    let pool = bss_db::connect(&url).await.expect("connect live Postgres");
    let a = || Arc::new(TokenAuthProvider::new(token()).unwrap());
    // Clients aren't exercised by the read routes; point them at the defaults.
    let state = AppState {
        pool,
        crm: CrmClient::new("http://localhost:8002", a()).unwrap(),
        catalog: CatalogClient::new("http://localhost:8001", a()).unwrap(),
        payment: PaymentClient::new("http://localhost:8007", a()).unwrap(),
        som: SomClient::new("http://localhost:8006", a()).unwrap(),
        subscription: SubscriptionClient::new("http://localhost:8005", a()).unwrap(),
        loyalty: None,
        settings: Settings::from_env(),
    };
    let token_map = Arc::new(TokenMap::single(&token()));
    let app = com::create_app(state, token_map);
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
    let rust = spawn_app().await;
    let oracle = oracle_url();
    let http = reqwest::Client::new();

    let (s, b) = get_json(&http, &rust, "/health").await;
    assert_eq!(s, 200);
    assert_eq!(b["service"], "com");

    // /ready requires a token — 401 without.
    let r = http.get(format!("{rust}/ready")).send().await.unwrap();
    assert_eq!(r.status(), 401, "/ready must require a token");

    // Pick a real order id from the live list to golden-diff the single-get.
    let (_, list) = get_json(
        &http,
        &oracle,
        "/tmf-api/productOrderingManagement/v4/productOrder?limit=1",
    )
    .await;
    let order_id = list
        .as_array()
        .and_then(|a| a.first())
        .and_then(|o| o.get("id"))
        .and_then(Value::as_str)
        .map(str::to_string);

    for path in [
        "/tmf-api/productOrderingManagement/v4/productOrder?limit=5".to_string(),
        "/tmf-api/productOrderingManagement/v4/productOrder?state=completed&limit=3".to_string(),
        "/tmf-api/productOrderingManagement/v4/productOrder/ORD-999999".to_string(),
    ] {
        golden(&http, &rust, &oracle, &path).await;
    }
    if let Some(oid) = order_id {
        golden(
            &http,
            &rust,
            &oracle,
            &format!("/tmf-api/productOrderingManagement/v4/productOrder/{oid}"),
        )
        .await;
        // list filtered by that order's customer.
        let (_, one) = get_json(
            &http,
            &oracle,
            &format!("/tmf-api/productOrderingManagement/v4/productOrder/{oid}"),
        )
        .await;
        if let Some(cust) = one.get("customerId").and_then(Value::as_str) {
            golden(
                &http,
                &rust,
                &oracle,
                &format!("/tmf-api/productOrderingManagement/v4/productOrder?customerId={cust}"),
            )
            .await;
        }
    }

    println!("com golden diff: read surface matches the Python oracle");
}
