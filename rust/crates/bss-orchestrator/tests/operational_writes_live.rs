//! Live smoke — the operational WRITE tools (inventory / port_request /
//! provisioning) against the running stack. `#[ignore]`:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test operational_writes_live -- --ignored --nocapture
//! ```
//!
//! All error/sentinel paths — proves each write body reaches validation without
//! mutating seed data: invalid inputs → structured errors, and
//! `set_fault_injection` with a bogus pair → the NOT_FOUND sentinel (which still
//! exercises the list→find composite against the live injector config). Read-mostly.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CrmClient, InventoryClient, ProvisioningClient, TokenAuthProvider};
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
async fn operational_write_bodies_reach_validation() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let crm_url = env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string());
    let crm = CrmClient::new(crm_url.clone(), auth.clone()).unwrap();
    let inventory = InventoryClient::new(crm_url, auth.clone()).unwrap();
    let provisioning = ProvisioningClient::new(
        env("BSS_PROVISIONING_URL").unwrap_or_else(|| "http://localhost:8010".to_string()),
        auth,
    )
    .unwrap();

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::inventory::register_inventory_write_tools(&mut reg, inventory);
    bss_orchestrator::tools::port_request::register_port_request_write_tools(&mut reg, crm);
    bss_orchestrator::tools::provisioning::register_provisioning_write_tools(
        &mut reg,
        provisioning,
    );

    // inventory.msisdn.add_range — an 8-digit prefix trips `sane_prefix` (must be
    // 4-7 digits), so nothing is added to the pool.
    expect_err(
        &reg,
        "inventory.msisdn.add_range",
        json!({ "prefix": "123456789", "count": 1 }),
    )
    .await;

    // port_request.create — an invalid direction is rejected before any row is
    // written.
    expect_err(
        &reg,
        "port_request.create",
        json!({ "direction": "sideways", "donor_carrier": "X", "donor_msisdn": "90009999", "requested_port_date": "2026-09-01" }),
    )
    .await;

    // port_request.approve / reject on bogus ids → structured errors.
    expect_err(
        &reg,
        "port_request.approve",
        json!({ "port_request_id": "POR-NOPE" }),
    )
    .await;
    expect_err(
        &reg,
        "port_request.reject",
        json!({ "port_request_id": "POR-NOPE", "reason": "smoke" }),
    )
    .await;

    // provisioning.resolve_stuck / retry_failed on bogus tasks → structured errors.
    expect_err(
        &reg,
        "provisioning.resolve_stuck",
        json!({ "task_id": "PTK-NOPE", "note": "smoke" }),
    )
    .await;
    expect_err(
        &reg,
        "provisioning.retry_failed",
        json!({ "task_id": "PTK-NOPE" }),
    )
    .await;

    // provisioning.set_fault_injection — bogus (task_type, fault_type) exercises the
    // list→find composite and returns the NOT_FOUND sentinel (no patch performed).
    let res = run(
        &reg,
        "provisioning.set_fault_injection",
        json!({ "task_type": "NO_SUCH_TASK", "fault_type": "fail_always", "enabled": true }),
    )
    .await
    .expect("set_fault_injection returns the sentinel, not an error");
    assert_eq!(res["error"], json!("NOT_FOUND"), "sentinel: {res}");
    assert!(res["message"]
        .as_str()
        .unwrap_or_default()
        .contains("NO_SUCH_TASK"));
}
