//! Live smoke — the CRM read tools against the running crm + subscription
//! services. `#[ignore]` so CI skips it; run with the stack up:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! export BSS_CRM_URL=http://localhost:8002 BSS_SUBSCRIPTION_URL=http://localhost:8006
//! cargo test -p bss-orchestrator --test customer_tools_live -- --ignored --nocapture
//! ```
//!
//! Same contract as `catalog_tools_live`: each verbatim tool returns the client
//! response (asserted equal to a direct client call). The `customer.get` composite
//! is checked structurally — it returns the customer doc with the synthetic
//! `_extras` key carrying subscriptions/cases/interactions. Byte-parity vs the
//! Python tool follows transitively from the P4 CRM service golden diff. Read-only.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CrmClient, SubscriptionClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn clients() -> (CrmClient, SubscriptionClient) {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let crm_url = env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string());
    let sub_url =
        env("BSS_SUBSCRIPTION_URL").unwrap_or_else(|| "http://localhost:8006".to_string());
    (
        CrmClient::new(crm_url, auth.clone()).unwrap(),
        SubscriptionClient::new(sub_url, auth).unwrap(),
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
async fn crm_read_tools_return_client_responses_verbatim() {
    let (crm, subscription) = clients();
    let mut registry = ToolRegistry::new();
    bss_orchestrator::tools::customer::register_customer_tools(
        &mut registry,
        crm.clone(),
        subscription.clone(),
    );

    // customer.list — tool output == direct client call, non-empty (seed data).
    let via_tool = call(&registry, "customer.list", json!({})).await;
    let via_client = crm.list_customers(None, None).await.unwrap();
    assert_eq!(
        via_tool, via_client,
        "customer.list must return the client response verbatim"
    );
    let list = via_tool.as_array().expect("customer list is an array");
    assert!(!list.is_empty(), "seed customers present");

    // Pick a real customer id to drive the id-scoped reads.
    let cid = list[0]["id"].as_str().expect("customer has id").to_string();

    // customer.get — the 360 composite: core record + synthetic `_extras`.
    let got = call(&registry, "customer.get", json!({ "customer_id": cid })).await;
    assert_eq!(got["id"], json!(cid), "get returns the requested customer");
    let extras = got.get("_extras").expect("_extras present on customer.get");
    for key in ["subscriptions", "cases", "interactions"] {
        assert!(extras[key].is_array(), "_extras.{key} is an array");
    }

    // customer.get_kyc_status — verbatim.
    let kyc = call(
        &registry,
        "customer.get_kyc_status",
        json!({ "customer_id": cid }),
    )
    .await;
    assert_eq!(kyc, crm.get_kyc_status(&cid).await.unwrap());

    // interaction.list — verbatim, explicit limit.
    let via_tool = call(
        &registry,
        "interaction.list",
        json!({ "customer_id": cid, "limit": 10 }),
    )
    .await;
    assert_eq!(via_tool, crm.list_interactions(&cid, 10).await.unwrap());
    assert!(via_tool.is_array());

    // customer.list with a name filter — verbatim (filter plumbs through).
    let via_tool = call(&registry, "customer.list", json!({ "name_contains": "a" })).await;
    assert_eq!(via_tool, crm.list_customers(None, Some("a")).await.unwrap());

    // Unknown customer → a CLIENT_ERROR observation (NotFound), not a panic.
    let tool = registry.get("customer.get").unwrap();
    let err = (tool.func)(json!({ "customer_id": "CUST-NOPE" }), ToolCtx::default())
        .await
        .expect_err("unknown customer should error");
    assert!(err.to_observation().contains("CLIENT_ERROR"));
}
