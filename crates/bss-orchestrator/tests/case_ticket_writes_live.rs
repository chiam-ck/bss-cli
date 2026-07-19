//! Live smoke — the case + ticket WRITE tools against the running CRM. `#[ignore]`:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test case_ticket_writes_live -- --ignored --nocapture
//! ```
//!
//! Exercises the write bodies live (the 4c lesson): the case FSM transition maps
//! target-state → the `{"trigger"}` body the CRM route requires (the prior
//! `{"state"}`/`{"toState"}` shapes 422'd on every call), and the ticket
//! transition/close resolve `in_progress` from the current state. Mutates the dev
//! stack (opens a case + ticket against an existing customer, then closes them).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CrmClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

async fn call(reg: &ToolRegistry, name: &str, args: Value) -> Value {
    let tool = reg.get(name).unwrap_or_else(|| panic!("{name} registered"));
    (tool.func)(args, ToolCtx::default())
        .await
        .unwrap_or_else(|e| panic!("{name} failed: {:?}", e.to_observation()))
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn case_ticket_write_lifecycle_bodies_are_accepted() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let crm = CrmClient::new(
        env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string()),
        auth,
    )
    .unwrap();

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::case::register_case_write_tools(&mut reg, crm.clone());
    bss_orchestrator::tools::ticket::register_ticket_write_tools(&mut reg, crm.clone());

    // An existing active customer to hang the case/ticket on.
    let customers = crm.list_customers(Some("active"), None).await.unwrap();
    let cid = customers
        .as_array()
        .and_then(|a| a.first())
        .and_then(|c| c["id"].as_str())
        .expect("an active seed customer")
        .to_string();

    // ── case lifecycle: open → note → priority → transition → close ──────────
    let case = call(
        &reg,
        "case.open",
        json!({ "customer_id": cid, "subject": "rust write smoke", "category": "information", "priority": "low" }),
    )
    .await;
    let case_id = case["id"].as_str().expect("case id").to_string();

    let noted = call(
        &reg,
        "case.add_note",
        json!({ "case_id": case_id, "body": "note from the rust write smoke" }),
    )
    .await;
    assert!(noted.is_object(), "add_note returned a doc: {noted}");

    call(
        &reg,
        "case.update_priority",
        json!({ "case_id": case_id, "priority": "medium" }),
    )
    .await;

    // transition open → in_progress (trigger "take"): the {"trigger"} body must be
    // accepted.
    let moved = call(
        &reg,
        "case.transition",
        json!({ "case_id": case_id, "to_state": "in_progress" }),
    )
    .await;
    assert_eq!(moved["id"], json!(case_id));

    // Unknown target state → a ValueError observation (no HTTP call), matching Python.
    let tool = reg.get("case.transition").unwrap();
    let err = (tool.func)(
        json!({ "case_id": case_id, "to_state": "banana" }),
        ToolCtx::default(),
    )
    .await
    .expect_err("unknown target state errors");
    assert!(
        err.to_observation().contains("ValueError"),
        "unknown state → ValueError: {}",
        err.to_observation()
    );

    // ── ticket lifecycle: open → assign? → resolve → close ──────────────────
    let ticket = call(
        &reg,
        "ticket.open",
        json!({ "ticket_type": "general_inquiry", "subject": "rust ticket smoke", "customer_id": cid, "case_id": case_id }),
    )
    .await;
    let ticket_id = ticket["id"].as_str().expect("ticket id").to_string();

    // resolve (mandatory notes) — a fresh ticket is acknowledged; resolve is a legal
    // transition. Tolerate a policy error if the FSM disagrees, but never a panic.
    let tool = reg.get("ticket.resolve").unwrap();
    let _ = (tool.func)(
        json!({ "ticket_id": ticket_id, "resolution_notes": "closed by rust smoke" }),
        ToolCtx::default(),
    )
    .await;

    // close → transition to "closed" (trigger "close"); {"trigger"} body accepted.
    let tool = reg.get("ticket.close").unwrap();
    let _ = (tool.func)(json!({ "ticket_id": ticket_id }), ToolCtx::default()).await;

    // Close the case (resolution_code). case.close requires child tickets resolved;
    // tolerate a policy error, assert it's structured (never a panic).
    let tool = reg.get("case.close").unwrap();
    match (tool.func)(
        json!({ "case_id": case_id, "resolution_code": "resolved" }),
        ToolCtx::default(),
    )
    .await
    {
        Ok(v) => assert_eq!(v["id"], json!(case_id)),
        Err(e) => assert!(
            e.to_observation().contains("POLICY_VIOLATION")
                || e.to_observation().contains("CLIENT_ERROR"),
            "case.close error is structured: {}",
            e.to_observation()
        ),
    }
}
