//! Live smoke — the operator read batch (order / SOM / inventory / provisioning /
//! usage / agents / events) against the running stack. `#[ignore]` so CI skips it:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test operator_reads_live -- --ignored --nocapture
//! ```
//!
//! One broad smoke for the whole batch (the batch-cadence philosophy): each verbatim
//! tool is asserted equal to a direct client call, so byte-parity follows
//! transitively from the P2–P4 service golden diffs. `events.list` is the v0.1
//! NOT_IMPLEMENTED stub (client-free). Read-only.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{
    ComClient, CrmClient, InventoryClient, MediationClient, ProvisioningClient, SomClient,
    SubscriptionClient, TokenAuthProvider,
};
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
async fn operator_read_batch_matches_direct_client_calls() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());

    let crm = CrmClient::new(url("BSS_CRM_URL", "http://localhost:8002"), auth.clone()).unwrap();
    let inv =
        InventoryClient::new(url("BSS_CRM_URL", "http://localhost:8002"), auth.clone()).unwrap();
    let com = ComClient::new(url("BSS_COM_URL", "http://localhost:8004"), auth.clone()).unwrap();
    let som = SomClient::new(url("BSS_SOM_URL", "http://localhost:8005"), auth.clone()).unwrap();
    let sub = SubscriptionClient::new(
        url("BSS_SUBSCRIPTION_URL", "http://localhost:8006"),
        auth.clone(),
    )
    .unwrap();
    let med = MediationClient::new(
        url("BSS_MEDIATION_URL", "http://localhost:8007"),
        auth.clone(),
    )
    .unwrap();
    let prov = ProvisioningClient::new(url("BSS_PROVISIONING_URL", "http://localhost:8010"), auth)
        .unwrap();

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::order::register_order_tools(&mut reg, com.clone());
    bss_orchestrator::tools::som::register_som_tools(&mut reg, som.clone());
    bss_orchestrator::tools::inventory::register_inventory_tools(&mut reg, inv.clone());
    bss_orchestrator::tools::provisioning::register_provisioning_tools(&mut reg, prov.clone());
    bss_orchestrator::tools::usage::register_usage_tools(&mut reg, med.clone());
    bss_orchestrator::tools::ops::register_ops_tools(&mut reg, crm.clone());

    // ── inventory ────────────────────────────────────────────────────────────
    let count = call(&reg, "inventory.msisdn.count", json!({})).await;
    assert_eq!(count, inv.count_msisdns(None).await.unwrap());

    let msisdns = call(
        &reg,
        "inventory.msisdn.list_available",
        json!({ "limit": 5 }),
    )
    .await;
    assert_eq!(
        msisdns,
        inv.list_msisdns(Some("available"), None, 5).await.unwrap()
    );
    if let Some(m) = msisdns.as_array().and_then(|a| a.first()) {
        let num = m["msisdn"].as_str().expect("msisdn field");
        let got = call(&reg, "inventory.msisdn.get", json!({ "msisdn": num })).await;
        assert_eq!(got, inv.get_msisdn(num).await.unwrap());
    }

    let esims = call(&reg, "inventory.esim.list_available", json!({ "limit": 5 })).await;
    assert_eq!(esims, inv.list_esims(Some("available"), 5).await.unwrap());

    // ── provisioning ─────────────────────────────────────────────────────────
    let tasks = call(&reg, "provisioning.list_tasks", json!({})).await;
    assert_eq!(tasks, prov.list_tasks(None, None).await.unwrap());

    // ── usage ────────────────────────────────────────────────────────────────
    let usage = call(&reg, "usage.history", json!({ "limit": 5 })).await;
    assert_eq!(
        usage,
        med.list_usage(None, None, None, None, 5).await.unwrap()
    );

    // ── agents ───────────────────────────────────────────────────────────────
    let agents = call(&reg, "agents.list", json!({})).await;
    assert_eq!(agents, crm.list_agents(None).await.unwrap());
    assert!(agents.is_array());

    // ── events (NOT_IMPLEMENTED stub) ─────────────────────────────────────────
    let events = call(&reg, "events.list", json!({ "limit": 10 })).await;
    assert_eq!(events["error"], json!("NOT_IMPLEMENTED"));
    assert_eq!(events["limit"], json!(10));

    // ── order + SOM (resolve a real chain via the first customer) ─────────────
    let customers = crm.list_customers(None, None).await.unwrap();
    let cid = customers[0]["id"]
        .as_str()
        .expect("customer id")
        .to_string();

    let orders = call(&reg, "order.list", json!({ "customer_id": cid })).await;
    assert_eq!(orders, com.list_orders(Some(&cid)).await.unwrap());

    if let Some(order) = orders.as_array().and_then(|a| a.first()) {
        let oid = order["id"].as_str().expect("order id").to_string();
        let got = call(&reg, "order.get", json!({ "order_id": oid })).await;
        assert_eq!(got, com.get_order(&oid).await.unwrap());

        // wait_until on the current state returns immediately (already there).
        if let Some(state) = got["state"].as_str() {
            let waited = call(
                &reg,
                "order.wait_until",
                json!({ "order_id": oid, "target_state": state, "timeout_s": 5.0 }),
            )
            .await;
            assert_eq!(waited["id"], json!(oid));
        }

        // SOM: service orders for this commercial order.
        let sos = call(
            &reg,
            "service_order.list_for_order",
            json!({ "commercial_order_id": oid }),
        )
        .await;
        assert_eq!(sos, som.list_for_order(&oid).await.unwrap());
        if let Some(so) = sos.as_array().and_then(|a| a.first()) {
            let soid = so["id"].as_str().expect("service order id");
            let got = call(
                &reg,
                "service_order.get",
                json!({ "service_order_id": soid }),
            )
            .await;
            assert_eq!(got, som.get_service_order(soid).await.unwrap());
        }
    }

    // SOM service reads via the customer's first subscription.
    let subs = sub.list_for_customer(&cid).await.unwrap();
    if let Some(s) = subs.as_array().and_then(|a| a.first()) {
        let sid = s["id"].as_str().expect("subscription id").to_string();
        let services = call(
            &reg,
            "service.list_for_subscription",
            json!({ "subscription_id": sid }),
        )
        .await;
        assert_eq!(
            services,
            som.list_services_for_subscription(&sid).await.unwrap()
        );
        if let Some(svc) = services.as_array().and_then(|a| a.first()) {
            let svc_id = svc["id"].as_str().expect("service id");
            let got = call(&reg, "service.get", json!({ "service_id": svc_id })).await;
            assert_eq!(got, som.get_service(svc_id).await.unwrap());
        }
    }

    // Unknown ids → a CLIENT_ERROR observation, not a panic (spot-check two families).
    for (name, args) in [
        ("order.get", json!({ "order_id": "ORD-NOPE" })),
        ("provisioning.get_task", json!({ "task_id": "PTK-NOPE" })),
    ] {
        let tool = reg.get(name).unwrap();
        let err = (tool.func)(args, ToolCtx::default())
            .await
            .expect_err("unknown id should error");
        assert!(
            err.to_observation().contains("CLIENT_ERROR"),
            "{name}: expected CLIENT_ERROR, got {}",
            err.to_observation()
        );
    }
}
