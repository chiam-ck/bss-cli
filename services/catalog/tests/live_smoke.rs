//! Live smoke + golden diff — the Phase-3 analogue of the conformance harness.
//! `#[ignore]` so CI skips it; run with the stack up (Python catalog on :8001):
//!
//! ```bash
//! set -a; source ../../.env; set +a          # from rust/services/catalog
//! export BSS_CATALOG_URL=http://localhost:8001
//! cargo test -p catalog --test live_smoke -- --ignored --nocapture
//! ```
//!
//! The core check is a **golden diff**: boot the Rust catalog in-process against
//! the same live Postgres + loyalty-cli, then assert each scenario-touched
//! endpoint's JSON is byte-equal (semantic `Value ==`, which is order-sensitive
//! for arrays — so allowance ordering and float rendering are covered) to the
//! live Python oracle's. Everything read-only; nothing mutated.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use bss_clients::{BearerAuthProvider, LoyaltyClient};
use bss_middleware::TokenMap;
use catalog::config::{normalize_db_url, Settings};
use catalog::state::AppState;
use serde_json::Value;

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn token() -> String {
    env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set")
}

fn oracle_url() -> String {
    env("BSS_CATALOG_URL").unwrap_or_else(|| "http://localhost:8001".to_string())
}

async fn spawn_app() -> String {
    let url = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    let pool = bss_db::connect(&url).await.expect("connect live Postgres");
    let loyalty = env("BSS_LOYALTY_API_TOKEN").map(|tok| {
        let auth = Arc::new(BearerAuthProvider::new(tok).unwrap());
        let base = env("BSS_LOYALTY_BASE_URL").unwrap_or_else(|| "http://loyalty-http:8080".into());
        LoyaltyClient::new(base, auth).unwrap()
    });
    let state = AppState {
        pool,
        loyalty,
        settings: Settings::from_env(),
    };
    let token_map = Arc::new(TokenMap::single(&token()));
    let app = catalog::create_app(state, token_map);
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

/// Assert the Rust surface matches the Python oracle byte-for-byte (semantic).
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

    // health shape (independent of oracle).
    let (s, b) = get_json(&http, &rust, "/health").await;
    assert_eq!(s, 200);
    assert_eq!(b["service"], "catalog");

    // /ready requires a token (only /health is exempt) — 401 without.
    let r = http.get(format!("{rust}/ready")).send().await.unwrap();
    assert_eq!(r.status(), 401, "/ready must require a token");

    // TMF620 reads — the fat surface every downstream consumer depends on.
    for path in [
        "/tmf-api/productCatalogManagement/v4/productOffering",
        "/tmf-api/productCatalogManagement/v4/productOffering?limit=2",
        "/tmf-api/productCatalogManagement/v4/productOffering?lifecycleStatus=active",
        "/tmf-api/productCatalogManagement/v4/productOffering?activeAt=2026-02-14T00:00:00Z",
        "/tmf-api/productCatalogManagement/v4/productOffering/PLAN_M",
        "/tmf-api/productCatalogManagement/v4/productOffering/PLAN_L",
        "/tmf-api/productCatalogManagement/v4/productOffering/PLAN_S",
        "/tmf-api/productCatalogManagement/v4/productOffering/NOPE",
        "/tmf-api/productCatalogManagement/v4/productOfferingPrice/active/PLAN_M",
        "/tmf-api/productCatalogManagement/v4/productOfferingPrice/active/PLAN_M?activeAt=2026-02-14T00:00:00Z",
        "/tmf-api/productCatalogManagement/v4/productOfferingPrice/PRICE_PLAN_M",
        "/tmf-api/productCatalogManagement/v4/productSpecification",
        "/tmf-api/productCatalogManagement/v4/productSpecification/SPEC_MOBILE_PREPAID",
        "/vas/offering",
        "/vas/offering/VAS_DATA_1GB",
    ] {
        golden(&http, &rust, &oracle, path).await;
    }

    // no-active-price 422 — checked apart from the golden loop because its
    // message/context carry `clock_now()`, which differs between two live calls.
    // (The `active/…?activeAt=…` variants above are deterministic and golden-diffed.)
    let (s, b) = get_json(
        &http,
        &rust,
        "/tmf-api/productCatalogManagement/v4/productOfferingPrice/active/NOPE",
    )
    .await;
    assert_eq!(s, 422);
    assert_eq!(b["code"], "POLICY_VIOLATION");
    assert_eq!(b["reason"], "catalog.price.no_active_row");
    assert_eq!(b["context"]["offering_id"], "NOPE");
    assert!(b["message"]
        .as_str()
        .unwrap()
        .starts_with("No active recurring price for offering NOPE at "));

    // Promotions — TMF671 + portal-facing reads (exercises the live loyalty saga
    // read path + the decimal/label wire shapes).
    for path in [
        "/tmf-api/promotionManagement/v4/promotion",
        "/tmf-api/promotionManagement/v4/promotion/PROMO_DEMO_WELCOME",
        "/promo/validate?code=DEMO_WELCOME10&offering=PLAN_M",
        "/promo/validate?code=NONEXISTENT&offering=PLAN_M",
        "/promo/preview?code=DEMO_WELCOME10&offering=PLAN_M",
        "/promo/customer-offers?customerId=CUST-001",
    ] {
        golden(&http, &rust, &oracle, path).await;
    }

    println!("catalog golden diff: all endpoints match the Python oracle");
}
