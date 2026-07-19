//! Live smoke — the subscription read tools against the running subscription
//! service. `#[ignore]` so CI skips it; run with the stack up:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! export BSS_SUBSCRIPTION_URL=http://localhost:8006 BSS_CRM_URL=http://localhost:8002
//! cargo test -p bss-orchestrator --test subscription_tools_live -- --ignored --nocapture
//! ```
//!
//! Same verbatim contract as the catalog/CRM smokes. Additionally pins **D9**: the
//! projected-dict `subscription.get_esim_activation` must serialize its five keys
//! in Python dict-literal order (`subscriptionId, iccid, msisdn, activationCode,
//! imsi`) — asserted on the serialized observation string, which the ReAct loop
//! feeds to the model. Read-only.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CrmClient, SubscriptionClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn clients() -> (SubscriptionClient, CrmClient) {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let sub_url =
        env("BSS_SUBSCRIPTION_URL").unwrap_or_else(|| "http://localhost:8006".to_string());
    let crm_url = env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string());
    (
        SubscriptionClient::new(sub_url, auth.clone()).unwrap(),
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

/// Resolve any subscription id from the seed data via the first customer's lines.
async fn any_subscription_id(sub: &SubscriptionClient, crm: &CrmClient) -> String {
    let customers = crm.list_customers(None, None).await.unwrap();
    for cust in customers.as_array().expect("customer list") {
        let cid = cust["id"].as_str().unwrap_or_default();
        let lines = sub.list_for_customer(cid).await.unwrap();
        if let Some(first) = lines.as_array().and_then(|a| a.first()) {
            if let Some(id) = first["id"].as_str() {
                return id.to_string();
            }
        }
    }
    panic!("no subscription found in seed data");
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn subscription_read_tools_return_client_responses_verbatim() {
    let (sub, crm) = clients();
    let mut registry = ToolRegistry::new();
    bss_orchestrator::tools::subscription::register_subscription_tools(&mut registry, sub.clone());

    let sid = any_subscription_id(&sub, &crm).await;

    // subscription.get — verbatim.
    let got = call(
        &registry,
        "subscription.get",
        json!({ "subscription_id": sid }),
    )
    .await;
    assert_eq!(got, sub.get(&sid).await.unwrap());
    assert_eq!(
        got["id"],
        json!(sid),
        "get returns the requested subscription"
    );

    // subscription.get_balance — verbatim.
    let bal = call(
        &registry,
        "subscription.get_balance",
        json!({ "subscription_id": sid }),
    )
    .await;
    assert_eq!(bal, sub.get_balance(&sid).await.unwrap());

    // subscription.list_for_customer — verbatim, via the sub's own customerId.
    let cid = got["customerId"]
        .as_str()
        .expect("subscription carries customerId")
        .to_string();
    let via_tool = call(
        &registry,
        "subscription.list_for_customer",
        json!({ "customer_id": cid }),
    )
    .await;
    assert_eq!(via_tool, sub.list_for_customer(&cid).await.unwrap());
    assert!(via_tool.as_array().is_some_and(|a| !a.is_empty()));

    // subscription.get_esim_activation — projected dict, verbatim vs the client.
    let esim = call(
        &registry,
        "subscription.get_esim_activation",
        json!({ "subscription_id": sid }),
    )
    .await;
    assert_eq!(esim, sub.get_esim_activation(&sid).await.unwrap());
    assert_eq!(esim["subscriptionId"], json!(sid));

    // D9 — the serialized observation must carry the five keys in Python
    // dict-literal insertion order, not alphabetical. This is what the ReAct loop
    // feeds the model, so it is the byte-parity contract the R2 gate depends on.
    let serialized = esim.to_string();
    let order: Vec<usize> = [
        "subscriptionId",
        "iccid",
        "msisdn",
        "activationCode",
        "imsi",
    ]
    .iter()
    .map(|k| {
        serialized
            .find(&format!("\"{k}\""))
            .unwrap_or_else(|| panic!("key {k} present in {serialized}"))
    })
    .collect();
    assert!(
        order.windows(2).all(|w| w[0] < w[1]),
        "esim keys out of Python insertion order (preserve_order regressed): {serialized}"
    );

    // Unknown subscription → CLIENT_ERROR, not a panic.
    let tool = registry.get("subscription.get").unwrap();
    let err = (tool.func)(json!({ "subscription_id": "SUB-NOPE" }), ToolCtx::default())
        .await
        .expect_err("unknown subscription should error");
    assert!(err.to_observation().contains("CLIENT_ERROR"));
}
