//! Live smoke — the catalog read tools against the running catalog service.
//! `#[ignore]` so CI skips it; run with the stack up:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! export BSS_CATALOG_URL=http://localhost:8001
//! cargo test -p bss-orchestrator --test catalog_tools_live -- --ignored --nocapture
//! ```
//!
//! Proves the client-backed tool pattern: the tool returns the `CatalogClient`
//! response verbatim (asserted equal to a direct client call), and the response
//! is real data. Byte-parity vs the Python tool follows transitively from the P3
//! catalog service golden diff. Read-only; nothing mutated.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CatalogClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn client() -> CatalogClient {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let url = env("BSS_CATALOG_URL").unwrap_or_else(|| "http://localhost:8001".to_string());
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    CatalogClient::new(url, auth).unwrap()
}

async fn call(registry: &ToolRegistry, name: &str, args: Value) -> Value {
    let tool = registry
        .get(name)
        .unwrap_or_else(|| panic!("{name} registered"));
    (tool.func)(args, ToolCtx::default())
        .await
        .unwrap_or_else(|e| panic!("{name} failed: {:?}", e.to_observation()))
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn catalog_read_tools_return_client_responses_verbatim() {
    let cat = client();
    let mut registry = ToolRegistry::new();
    bss_orchestrator::tools::catalog::register_catalog_tools(&mut registry, cat.clone());

    // list_offerings — tool output == direct client call, and it's a non-empty list.
    let via_tool = call(&registry, "catalog.list_offerings", json!({})).await;
    let via_client = cat.list_offerings().await.unwrap();
    assert_eq!(
        via_tool, via_client,
        "tool must return the client response verbatim"
    );
    assert!(
        via_tool.as_array().is_some_and(|a| !a.is_empty()),
        "offerings present"
    );

    // list_vas — same verbatim contract.
    let via_tool = call(&registry, "catalog.list_vas", json!({})).await;
    assert_eq!(via_tool, cat.list_vas().await.unwrap());
    assert!(via_tool.as_array().is_some());

    // get_offering(PLAN_M) — real offering document.
    let offering = call(
        &registry,
        "catalog.get_offering",
        json!({ "offering_id": "PLAN_M" }),
    )
    .await;
    assert_eq!(
        offering["id"], "PLAN_M",
        "get_offering returns the right doc"
    );
    assert_eq!(offering, cat.get_offering("PLAN_M").await.unwrap());

    // list_active_offerings(explicit at) — deterministic against a direct call.
    let at = "2026-07-13T00:00:00+00:00";
    let via_tool = call(
        &registry,
        "catalog.list_active_offerings",
        json!({ "at": at }),
    )
    .await;
    assert_eq!(via_tool, cat.list_active_offerings(at).await.unwrap());
    assert!(via_tool.as_array().is_some_and(|a| !a.is_empty()));

    // get_active_price(PLAN_M) — real price row.
    let price = call(
        &registry,
        "catalog.get_active_price",
        json!({ "offering_id": "PLAN_M" }),
    )
    .await;
    assert_eq!(price, cat.get_active_price("PLAN_M").await.unwrap());
    assert!(price.get("id").is_some() || price.get("price").is_some());

    // Unknown offering → a CLIENT_ERROR observation (NotFound), not a panic.
    let tool = registry.get("catalog.get_offering").unwrap();
    let err = (tool.func)(json!({ "offering_id": "PLAN_NOPE" }), ToolCtx::default())
        .await
        .expect_err("unknown offering should error");
    assert!(err.to_observation().contains("CLIENT_ERROR"));
}
