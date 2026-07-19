//! Live smoke — the CRM/catalog read batch (ticket / case / promo / port_request)
//! against the running stack. `#[ignore]` so CI skips it:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test crm_reads_live -- --ignored --nocapture
//! ```
//!
//! Each verbatim tool is asserted equal to a direct client call. `case.show_
//! transcript_for` is a composite — checked structurally (either a transcript body
//! or the `no_transcript_linked` sentinel). Read-only.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CatalogClient, CrmClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn url(key: &str, default: &str) -> String {
    env(key).unwrap_or_else(|| default.to_string())
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
async fn crm_catalog_read_batch_matches_direct_client_calls() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let crm = CrmClient::new(url("BSS_CRM_URL", "http://localhost:8002"), auth.clone()).unwrap();
    let catalog =
        CatalogClient::new(url("BSS_CATALOG_URL", "http://localhost:8001"), auth).unwrap();

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::ticket::register_ticket_tools(&mut reg, crm.clone());
    bss_orchestrator::tools::case::register_case_tools(&mut reg, crm.clone());
    bss_orchestrator::tools::port_request::register_port_request_tools(&mut reg, crm.clone());
    bss_orchestrator::tools::promo::register_promo_tools(&mut reg, catalog.clone());

    // ── tickets ────────────────────────────────────────────────────────────
    let tickets = call(&reg, "ticket.list", json!({})).await;
    assert_eq!(
        tickets,
        crm.list_tickets(None, None, None, None).await.unwrap()
    );
    if let Some(t) = tickets.as_array().and_then(|a| a.first()) {
        let tid = t["id"].as_str().expect("ticket id");
        let got = call(&reg, "ticket.get", json!({ "ticket_id": tid })).await;
        assert_eq!(got, crm.get_ticket(tid).await.unwrap());
    }

    // ── cases ──────────────────────────────────────────────────────────────
    let cases = call(&reg, "case.list", json!({})).await;
    assert_eq!(cases, crm.list_cases(None, None, None).await.unwrap());
    if let Some(cs) = cases.as_array().and_then(|a| a.first()) {
        let case_id = cs["id"].as_str().expect("case id").to_string();
        let got = call(&reg, "case.get", json!({ "case_id": case_id })).await;
        assert_eq!(got, crm.get_case(&case_id).await.unwrap());

        // show_transcript_for — a transcript body or the sentinel, never a panic.
        let tr = call(
            &reg,
            "case.show_transcript_for",
            json!({ "case_id": case_id }),
        )
        .await;
        assert!(
            tr.get("transcript").is_some(),
            "expected a transcript or the no_transcript_linked sentinel, got {tr}"
        );
    }

    // ── port requests ──────────────────────────────────────────────────────
    let ports = call(&reg, "port_request.list", json!({ "limit": 5 })).await;
    assert_eq!(
        ports,
        crm.list_port_requests(None, None, 5, 0).await.unwrap()
    );
    if let Some(p) = ports.as_array().and_then(|a| a.first()) {
        let pid = p["id"].as_str().expect("port request id");
        let got = call(&reg, "port_request.get", json!({ "port_request_id": pid })).await;
        assert_eq!(got, crm.get_port_request(pid).await.unwrap());
    }

    // ── promo (unknown id → CLIENT_ERROR; a live promo id isn't guaranteed) ──
    let tool = reg.get("promo.show").unwrap();
    let err = (tool.func)(json!({ "promotion_id": "PROMO-NOPE" }), ToolCtx::default())
        .await
        .expect_err("unknown promotion should error");
    assert!(err.to_observation().contains("CLIENT_ERROR"));

    // Unknown ticket → CLIENT_ERROR, not a panic.
    let tool = reg.get("ticket.get").unwrap();
    let err = (tool.func)(json!({ "ticket_id": "TKT-NOPE" }), ToolCtx::default())
        .await
        .expect_err("unknown ticket should error");
    assert!(err.to_observation().contains("CLIENT_ERROR"));
}
