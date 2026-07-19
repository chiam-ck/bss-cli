//! Live smoke — the subscription WRITE tools against the running stack. `#[ignore]`:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test subscription_writes_live -- --ignored --nocapture
//! ```
//!
//! Money-movers, so this stays conservative: one **reversible** schedule→cancel
//! round-trip on a real subscription (pending fields set then cleared), plus the
//! structured-error paths for the destructive/charging writes against bogus ids —
//! enough to prove the request bodies reach validation without wrecking seed data.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CrmClient, SubscriptionClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolError, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

async fn run(reg: &ToolRegistry, name: &str, args: Value) -> Result<Value, ToolError> {
    let tool = reg.get(name).unwrap_or_else(|| panic!("{name} registered"));
    (tool.func)(args, ToolCtx::default()).await
}

async fn expect_err(reg: &ToolRegistry, name: &str, args: Value) {
    let err = run(reg, name, args)
        .await
        .expect_err("expected an error observation");
    let obs = err.to_observation();
    assert!(
        obs.contains("CLIENT_ERROR") || obs.contains("POLICY_VIOLATION"),
        "{name}: expected a structured error, got {obs}"
    );
}

#[tokio::test]
#[ignore = "hits the live stack; run with --ignored"]
async fn subscription_write_bodies_reach_validation() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let sub = SubscriptionClient::new(
        env("BSS_SUBSCRIPTION_URL").unwrap_or_else(|| "http://localhost:8006".to_string()),
        auth.clone(),
    )
    .unwrap();
    let crm = CrmClient::new(
        env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string()),
        auth,
    )
    .unwrap();

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::subscription::register_subscription_write_tools(&mut reg, sub.clone());

    // Find a real active subscription (via the first customer that has one).
    let customers = crm.list_customers(Some("active"), None).await.unwrap();
    let mut real_sub: Option<String> = None;
    for cust in customers.as_array().unwrap_or(&Vec::new()) {
        let cid = cust["id"].as_str().unwrap_or_default();
        let lines = sub.list_for_customer(cid).await.unwrap();
        if let Some(s) = lines.as_array().and_then(|a| {
            a.iter()
                .find(|s| s["state"] == json!("active"))
                .or_else(|| a.first())
        }) {
            if let Some(id) = s["id"].as_str() {
                real_sub = Some(id.to_string());
                break;
            }
        }
    }
    let sid = real_sub.expect("an existing subscription in seed data");

    // Reversible round-trip: schedule a plan change (tolerate same-offering /
    // eligibility policy errors), then cancel — cancel is idempotent and clears any
    // pending fields we may have set, leaving the subscription as we found it.
    let _ = run(
        &reg,
        "subscription.schedule_plan_change",
        json!({ "subscription_id": sid, "new_offering_id": "PLAN_L" }),
    )
    .await;
    let cancelled = run(
        &reg,
        "subscription.cancel_pending_plan_change",
        json!({ "subscription_id": sid }),
    )
    .await
    .expect("cancel_pending_plan_change is idempotent and should succeed");
    assert_eq!(
        cancelled["id"],
        json!(sid),
        "cancel returns the subscription"
    );

    // Destructive / charging writes → structured errors against bogus ids (no real
    // termination, renewal charge, or VAS purchase performed).
    expect_err(
        &reg,
        "subscription.terminate",
        json!({ "subscription_id": "SUB-NOPE" }),
    )
    .await;
    expect_err(
        &reg,
        "subscription.renew_now",
        json!({ "subscription_id": "SUB-NOPE" }),
    )
    .await;
    expect_err(
        &reg,
        "subscription.purchase_vas",
        json!({ "subscription_id": "SUB-NOPE", "vas_offering_id": "VAS_DATA_1GB" }),
    )
    .await;
    expect_err(
        &reg,
        "subscription.migrate_to_new_price",
        json!({ "offering_id": "PLAN_NOPE", "new_price_id": "POP-NOPE", "effective_from": "2026-09-01T00:00:00+00:00" }),
    )
    .await;

    // tick_renewals_now — the deterministic sweep. Gated by BSS_ALLOW_ADMIN_RESET;
    // tolerate both a 403 (disabled) and an ok sweep. Never a panic.
    let _ = run(&reg, "subscription.tick_renewals_now", json!({})).await;
}
