//! Live smoke — the payment read tools against the running payment service.
//! `#[ignore]` so CI skips it; run with the stack up:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! export BSS_PAYMENT_URL=http://localhost:8003 BSS_CRM_URL=http://localhost:8002
//! cargo test -p bss-orchestrator --test payment_tools_live -- --ignored --nocapture
//! ```
//!
//! Same verbatim contract as the other tool smokes: each read tool returns the
//! `PaymentClient` response (asserted equal to a direct client call). Byte-parity
//! vs the Python tool follows transitively from the P4 payment golden diff.
//! Read-only.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CrmClient, PaymentClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn clients() -> (PaymentClient, CrmClient) {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let pay_url = env("BSS_PAYMENT_URL").unwrap_or_else(|| "http://localhost:8003".to_string());
    let crm_url = env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string());
    (
        PaymentClient::new(pay_url, auth.clone()).unwrap(),
        CrmClient::new(crm_url, auth).unwrap(),
    )
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
async fn payment_read_tools_return_client_responses_verbatim() {
    let (pay, crm) = clients();
    let mut registry = ToolRegistry::new();
    bss_orchestrator::tools::payment::register_payment_tools(&mut registry, pay.clone());

    // The service's list route requires customerId (Python `customerId: str`, no
    // default — the tool omits it when None, so an unfiltered call 400s on BOTH;
    // faithful parity). Resolve a real customer to drive the reads.
    let customers = crm.list_customers(None, None).await.unwrap();
    let cid = customers[0]["id"]
        .as_str()
        .expect("customer id")
        .to_string();

    // payment.list_methods — verbatim (filter plumbs through).
    let methods = call(
        &registry,
        "payment.list_methods",
        json!({ "customer_id": cid }),
    )
    .await;
    assert_eq!(methods, pay.list_methods(&cid).await.unwrap());
    assert!(methods.is_array());

    // payment.list_attempts filtered by that customer — verbatim.
    let via_tool = call(
        &registry,
        "payment.list_attempts",
        json!({ "customer_id": cid, "limit": 10 }),
    )
    .await;
    assert_eq!(
        via_tool,
        pay.list_payments(Some(&cid), None, 10).await.unwrap()
    );
    let attempts = via_tool.as_array().expect("attempts is an array");

    // payment.get_attempt on a real id (if the customer has any) — verbatim.
    if let Some(id) = attempts.first().and_then(|a| a["id"].as_str()) {
        let got = call(
            &registry,
            "payment.get_attempt",
            json!({ "attempt_id": id }),
        )
        .await;
        assert_eq!(got, pay.get_payment(id).await.unwrap());
        assert_eq!(got["id"], json!(id));
    }

    // Unknown attempt → CLIENT_ERROR, not a panic.
    let tool = registry.get("payment.get_attempt").unwrap();
    let err = (tool.func)(json!({ "attempt_id": "PAY-NOPE" }), ToolCtx::default())
        .await
        .expect_err("unknown attempt should error");
    assert!(err.to_observation().contains("CLIENT_ERROR"));
}
