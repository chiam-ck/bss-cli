//! Live smoke — the order + payment WRITE tools against the running stack.
//! `#[ignore]`:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test order_payment_writes_live -- --ignored --nocapture
//! ```
//!
//! Conservative on the money-movers: one real `payment.add_card` (exercises the
//! tokenizer + create_payment_method body) then `remove_method` to clean it up;
//! `order.create` is driven to a **policy error** (fresh customer has no KYC) so the
//! create+submit composite reaches validation without provisioning a line/charging a
//! card; charge/cancel error paths use bogus ids.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use bss_clients::{ComClient, CrmClient, PaymentClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolError, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

async fn run(reg: &ToolRegistry, name: &str, args: Value) -> Result<Value, ToolError> {
    let tool = reg.get(name).unwrap_or_else(|| panic!("{name} registered"));
    (tool.func)(args, ToolCtx::default()).await
}

async fn ok(reg: &ToolRegistry, name: &str, args: Value) -> Value {
    run(reg, name, args)
        .await
        .unwrap_or_else(|e| panic!("{name} failed: {:?}", e.to_observation()))
}

async fn expect_structured_err(reg: &ToolRegistry, name: &str, args: Value) {
    let obs = run(reg, name, args)
        .await
        .expect_err("expected a structured error")
        .to_observation();
    assert!(
        obs.contains("CLIENT_ERROR") || obs.contains("POLICY_VIOLATION"),
        "{name}: expected a structured error, got {obs}"
    );
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn order_payment_write_bodies_reach_validation() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let crm = CrmClient::new(
        env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string()),
        auth.clone(),
    )
    .unwrap();
    let payment = PaymentClient::new(
        env("BSS_PAYMENT_URL").unwrap_or_else(|| "http://localhost:8003".to_string()),
        auth.clone(),
    )
    .unwrap();
    let com = ComClient::new(
        env("BSS_COM_URL").unwrap_or_else(|| "http://localhost:8004".to_string()),
        auth,
    )
    .unwrap();

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::payment::register_payment_write_tools(&mut reg, payment.clone());
    bss_orchestrator::tools::order::register_order_write_tools(&mut reg, com);

    // Fresh customer (no KYC) to attach a card to + drive order.create to a policy stop.
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let created = crm
        .create_customer(
            "Order Pay Smoke",
            Some(&format!("orderpay{nonce}@example.com")),
            None,
        )
        .await
        .unwrap();
    let cid = created["id"].as_str().expect("customer id").to_string();

    // payment.add_card — the tokenizer + create_payment_method body end-to-end.
    let method = ok(
        &reg,
        "payment.add_card",
        json!({ "customer_id": cid, "card_number": "4111111111111111" }),
    )
    .await;
    // The create body was accepted → a real method doc with an id. (The tokenizer's
    // last4/brand logic is pinned by the unit test; response key shape is the P4
    // payment golden's concern.)
    let method_id = method["id"]
        .as_str()
        .expect("payment method id created")
        .to_string();

    // order.create — a bogus offering fails synchronously at create (catalog lookup),
    // so the create+submit composite reaches validation without provisioning a real
    // line (COF/KYC are checked async during provisioning, so a valid offering would
    // return `acknowledged` and actually reserve inventory — deliberately avoided).
    expect_structured_err(
        &reg,
        "order.create",
        json!({ "customer_id": cid, "offering_id": "PLAN_NOPE" }),
    )
    .await;

    // order.cancel + payment.charge → structured errors against bogus ids.
    expect_structured_err(&reg, "order.cancel", json!({ "order_id": "ORD-NOPE" })).await;
    expect_structured_err(
        &reg,
        "payment.charge",
        json!({ "customer_id": cid, "payment_method_id": "PM-NOPE", "amount": "1.00", "purpose": "smoke" }),
    )
    .await;

    // payment.remove_method — clean up the card we added (also exercises remove).
    let removed = ok(
        &reg,
        "payment.remove_method",
        json!({ "method_id": method_id }),
    )
    .await;
    assert!(
        removed.get("removed").is_some() || removed.get("id").is_some(),
        "remove returned a result: {removed}"
    );
}
