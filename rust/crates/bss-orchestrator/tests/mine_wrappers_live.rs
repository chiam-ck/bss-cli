//! Live smoke — the customer_self_serve `*.mine` wrappers against the running stack.
//! `#[ignore]`:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test mine_wrappers_live -- --ignored --nocapture
//! ```
//!
//! Proves the containment layer: unbound ctx → `_NoActorBound`; a bound actor reads
//! only its own data (annotated with the current charge); a subscription owned by a
//! DIFFERENT customer → `_NotOwnedByActor`. Read-only (no `*.mine` write is invoked).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{
    CrmClient, MediationClient, PaymentClient, SubscriptionClient, TokenAuthProvider,
};
use bss_orchestrator::{ToolCtx, ToolError, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn bound(actor: &str) -> ToolCtx {
    ToolCtx {
        actor: actor.to_string(),
        channel: "chat".to_string(),
        tenant: "DEFAULT".to_string(),
        transcript: "user:\nhi".to_string(),
    }
}

async fn call(
    reg: &ToolRegistry,
    name: &str,
    args: Value,
    ctx: ToolCtx,
) -> Result<Value, ToolError> {
    let tool = reg.get(name).unwrap_or_else(|| panic!("{name} registered"));
    (tool.func)(args, ctx).await
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn mine_wrappers_bind_and_gate_ownership() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = || Arc::new(TokenAuthProvider::new(token.clone()).unwrap());
    let crm_url = env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string());
    let sub_url =
        env("BSS_SUBSCRIPTION_URL").unwrap_or_else(|| "http://localhost:8006".to_string());
    let crm = CrmClient::new(crm_url, auth()).unwrap();
    let sub = SubscriptionClient::new(sub_url, auth()).unwrap();
    let payment = PaymentClient::new(
        env("BSS_PAYMENT_URL").unwrap_or_else(|| "http://localhost:8003".to_string()),
        auth(),
    )
    .unwrap();
    let mediation = MediationClient::new(
        env("BSS_MEDIATION_URL").unwrap_or_else(|| "http://localhost:8007".to_string()),
        auth(),
    )
    .unwrap();

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::mine::register_customer_self_serve_tools(
        &mut reg,
        sub.clone(),
        crm.clone(),
        payment,
        mediation,
    );

    // Collect (customer_id, subscription_id) pairs from the seed data.
    let customers = crm.list_customers(None, None).await.unwrap();
    let mut pairs: Vec<(String, String)> = Vec::new();
    for cust in customers.as_array().unwrap_or(&Vec::new()) {
        let cid = cust["id"].as_str().unwrap_or_default().to_string();
        for s in sub
            .list_for_customer(&cid)
            .await
            .unwrap()
            .as_array()
            .unwrap_or(&Vec::new())
        {
            if let Some(sid) = s["id"].as_str() {
                pairs.push((cid.clone(), sid.to_string()));
            }
        }
    }
    let (actor, my_sub) = pairs
        .first()
        .expect("a customer with a subscription")
        .clone();

    // Unbound (default ctx, actor="system") → the containment error.
    let err = call(
        &reg,
        "subscription.list_mine",
        json!({}),
        ToolCtx::default(),
    )
    .await
    .expect_err("unbound must error");
    assert!(
        err.to_observation().contains("_NoActorBound"),
        "{}",
        err.to_observation()
    );

    // Bound → the actor's own data.
    let mine = call(&reg, "subscription.list_mine", json!({}), bound(&actor))
        .await
        .expect("list_mine");
    let arr = mine.as_array().expect("list is an array");
    assert!(!arr.is_empty(), "actor has ≥1 subscription");
    assert!(
        arr.iter().all(|s| s.get("currentMonthlyCharge").is_some()),
        "each subscription is pricing-annotated"
    );

    let me = call(&reg, "customer.get_mine", json!({}), bound(&actor))
        .await
        .expect("get_mine");
    assert_eq!(
        me["id"],
        json!(actor),
        "get_mine returns the bound customer"
    );

    call(&reg, "payment.method_list_mine", json!({}), bound(&actor))
        .await
        .expect("method_list_mine");
    call(&reg, "case.list_for_me", json!({}), bound(&actor))
        .await
        .expect("case.list_for_me");

    // Owned subscription → get_mine succeeds and is annotated.
    let owned = call(
        &reg,
        "subscription.get_mine",
        json!({ "subscription_id": my_sub }),
        bound(&actor),
    )
    .await
    .expect("get_mine on owned sub");
    assert!(owned.get("currentMonthlyCharge").is_some());

    // A subscription owned by a DIFFERENT customer → _NotOwnedByActor.
    if let Some((_, other_sub)) = pairs.iter().find(|(cid, _)| *cid != actor) {
        let err = call(
            &reg,
            "subscription.get_mine",
            json!({ "subscription_id": other_sub }),
            bound(&actor),
        )
        .await
        .expect_err("cross-customer access must be blocked");
        assert!(
            err.to_observation().contains("_NotOwnedByActor"),
            "cross-customer → not_owned_by_actor: {}",
            err.to_observation()
        );
    }
}
