//! Live smoke — the trace + knowledge tools against the running stack. `#[ignore]`:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test trace_knowledge_live -- --ignored --nocapture
//! ```
//!
//! Trace + knowledge are the two infra-heavy read families (Jaeger/audit clients;
//! a Postgres FTS pool). Structural assertions — the corpus/traces are live data —
//! plus the deterministic error/sentinel paths. Read-only.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{AuditClient, JaegerClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn normalize_db_url(raw: &str) -> String {
    raw.replace("postgresql+asyncpg://", "postgres://")
        .replace("postgresql://", "postgres://")
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
async fn trace_tools_resolve_and_summarize() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let jaeger = JaegerClient::from_env().unwrap();
    let audit_com = AuditClient::new(
        env("BSS_COM_URL").unwrap_or_else(|| "http://localhost:8004".to_string()),
        auth.clone(),
    )
    .unwrap();
    let audit_sub = AuditClient::new(
        env("BSS_SUBSCRIPTION_URL").unwrap_or_else(|| "http://localhost:8006".to_string()),
        auth,
    )
    .unwrap();

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::trace::register_trace_tools(&mut reg, jaeger, audit_com, audit_sub);

    // trace.get on a bogus id → structured JAEGER_ERROR (never a turn failure),
    // regardless of whether Jaeger is reachable.
    let got = call(
        &reg,
        "trace.get",
        json!({ "trace_id": "00000000000000000000000000000000" }),
    )
    .await;
    assert_eq!(got["error"], json!("JAEGER_ERROR"));
    assert_eq!(got["traceId"], json!("00000000000000000000000000000000"));

    // trace.for_order on a bogus order → audit returns no events → NO_TRACE_RECORDED
    // (exercises the AuditClient path + the sentinel deterministically).
    let got = call(&reg, "trace.for_order", json!({ "order_id": "ORD-NOPE" })).await;
    assert_eq!(got["orderId"], json!("ORD-NOPE"));
    // Either the no-trace sentinel, or (if a stray trace existed) a summary.
    assert!(got.get("error").is_some() || got.get("spanCount").is_some());

    let got = call(
        &reg,
        "trace.for_subscription",
        json!({ "subscription_id": "SUB-NOPE" }),
    )
    .await;
    assert_eq!(got["subscriptionId"], json!("SUB-NOPE"));
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn knowledge_tools_search_and_get() {
    let url = normalize_db_url(&env("BSS_DB_URL").expect("BSS_DB_URL must be set"));
    let pool = bss_db::connect(&url).await.expect("connect live Postgres");

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::knowledge::register_knowledge_tools(&mut reg, pool);

    // knowledge.search — the wrapped shape {hits, query}; content is live data.
    let res = call(
        &reg,
        "knowledge.search",
        json!({ "query": "rotate api token", "k": 3 }),
    )
    .await;
    assert!(res["hits"].is_array(), "search returns a hits array");
    assert_eq!(res["query"], json!("rotate api token"));

    // If the corpus is indexed, fetch the first hit back via knowledge.get.
    if let Some(hit) = res["hits"].as_array().and_then(|a| a.first()) {
        let anchor = hit["anchor"].as_str().expect("hit anchor");
        let source_path = hit["source_path"].as_str().expect("hit source_path");
        let got = call(
            &reg,
            "knowledge.get",
            json!({ "anchor": anchor, "source_path": source_path }),
        )
        .await;
        assert_eq!(got["anchor"], json!(anchor));
        assert!(got.get("content").is_some(), "chunk carries content");
    }

    // knowledge.get on a bogus chunk → the NOT_FOUND sentinel.
    let miss = call(
        &reg,
        "knowledge.get",
        json!({ "anchor": "nope-nope", "source_path": "docs/NOPE.md" }),
    )
    .await;
    assert_eq!(miss["error"], json!("NOT_FOUND"));
    assert!(miss["message"]
        .as_str()
        .unwrap_or_default()
        .contains("nope-nope"));
}
