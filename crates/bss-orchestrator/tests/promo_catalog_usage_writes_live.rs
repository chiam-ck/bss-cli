//! Live smoke — the last writes (promo saga / catalog admin / usage.simulate)
//! against the running stack. `#[ignore]`:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test promo_catalog_usage_writes_live -- --ignored --nocapture
//! ```
//!
//! Error paths only — proves each write body reaches validation without creating a
//! real promotion / offering / usage row: invalid or bogus inputs → structured
//! errors. Response/body parity is transitive from the P3 catalog + P2 mediation
//! goldens.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CatalogClient, MediationClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolError, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

async fn expect_err(reg: &ToolRegistry, name: &str, args: Value) {
    let tool = reg.get(name).unwrap_or_else(|| panic!("{name} registered"));
    let obs = (tool.func)(args, ToolCtx::default())
        .await
        .err()
        .map(|e: ToolError| e.to_observation())
        .expect("expected a structured error, got Ok");
    assert!(
        obs.contains("CLIENT_ERROR") || obs.contains("POLICY_VIOLATION"),
        "{name}: expected a structured error, got {obs}"
    );
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn last_write_bodies_reach_validation() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let catalog = CatalogClient::new(
        env("BSS_CATALOG_URL").unwrap_or_else(|| "http://localhost:8001".to_string()),
        auth.clone(),
    )
    .unwrap();
    let mediation = MediationClient::new(
        env("BSS_MEDIATION_URL").unwrap_or_else(|| "http://localhost:8007".to_string()),
        auth,
    )
    .unwrap();

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::promo::register_promo_write_tools(&mut reg, catalog.clone());
    bss_orchestrator::tools::catalog::register_catalog_admin_write_tools(&mut reg, catalog);
    bss_orchestrator::tools::usage::register_usage_write_tools(&mut reg, mediation);

    // promo.create — `multi` duration without periods_total is rejected before the
    // saga persists anything.
    expect_err(
        &reg,
        "promo.create",
        json!({ "promotion_id": "PROMO-SMOKE", "discount_type": "percent", "discount_value": "10", "duration_kind": "multi", "audience": "public", "code": "SMOKECODE" }),
    )
    .await;

    // promo.assign on a non-existent promotion → structured error.
    expect_err(
        &reg,
        "promo.assign",
        json!({ "promotion_id": "PROMO-NOPE", "customer_ids": ["CUST-nope"] }),
    )
    .await;

    // catalog admin (LLM-hidden) — bogus offering → structured errors, nothing added.
    expect_err(
        &reg,
        "catalog.add_price",
        json!({ "offering_id": "PLAN_NOPE", "price_id": "POP-SMOKE", "amount": "9.99" }),
    )
    .await;
    expect_err(
        &reg,
        "catalog.window_offering",
        json!({ "offering_id": "PLAN_NOPE", "valid_to": "2027-01-01T00:00:00+00:00" }),
    )
    .await;

    // usage.simulate (LLM-hidden) — an unknown MSISDN trips mediation's block-at-edge
    // (subscription_must_exist), so no usage row is recorded.
    expect_err(
        &reg,
        "usage.simulate",
        json!({ "msisdn": "00000000", "event_type": "data", "quantity": 1, "unit": "mb" }),
    )
    .await;
}
