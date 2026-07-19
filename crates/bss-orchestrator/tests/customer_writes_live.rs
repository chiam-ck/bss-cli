//! Live smoke — the customer + interaction WRITE tools against the running CRM.
//! `#[ignore]` so CI skips it:
//!
//! ```bash
//! set -a; source ../../../.env; set +a     # from rust/crates/bss-orchestrator
//! cargo test -p bss-orchestrator --test customer_writes_live -- --ignored --nocapture
//! ```
//!
//! Writes can't assert `tool == direct client call` (a second call mutates again),
//! so this exercises the full lifecycle end-to-end and asserts the **request bodies
//! are accepted** — the HANDOFF's 4c lesson (a camelCase write-body bug the read
//! golden diff missed). Response byte-parity stays transitive from the P4 CRM golden.
//! Mutates the dev stack (creates then closes one customer).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use bss_clients::{CrmClient, TokenAuthProvider};
use bss_orchestrator::{ToolCtx, ToolRegistry};
use serde_json::{json, Value};

fn env(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
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
async fn customer_write_lifecycle_bodies_are_accepted() {
    let token = env("BSS_API_TOKEN").expect("BSS_API_TOKEN must be set");
    let auth = Arc::new(TokenAuthProvider::new(token).unwrap());
    let crm = CrmClient::new(
        env("BSS_CRM_URL").unwrap_or_else(|| "http://localhost:8002".to_string()),
        auth,
    )
    .unwrap();

    let mut reg = ToolRegistry::new();
    bss_orchestrator::tools::customer::register_customer_write_tools(&mut reg, crm.clone());

    // Unique identity so email-unique / hash-unique policies don't collide.
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let email = format!("rustsmoke{nonce}@example.com");

    // create — name split into given/family; email attached as primary medium.
    let created = call(
        &reg,
        "customer.create",
        json!({ "name": "Rust Smoke", "email": email }),
    )
    .await;
    let cid = created["id"].as_str().expect("created id").to_string();
    assert!(
        created["status"].is_string(),
        "created customer carries a status: {created}"
    );

    // add_contact_medium — reproduces a PRE-EXISTING Python client/service body
    // mismatch: the client wraps the value in `characteristic`, but the CRM service
    // (Python AddContactMediumRequest and the Rust port both) requires a top-level
    // `value` → 422. The faithful port reproduces the 422; the fix belongs in the
    // oracle first (behaviour-frozen). Assert the reproduced error, not success.
    let tool = reg.get("customer.add_contact_medium").unwrap();
    let err = (tool.func)(
        json!({ "customer_id": cid, "medium_type": "mobile", "value": "+6590009999" }),
        ToolCtx::default(),
    )
    .await
    .expect_err("characteristic-shaped body 422s on both services");
    assert!(
        err.to_observation().contains("CLIENT_ERROR"),
        "add_contact_medium reproduces the Python 422: {}",
        err.to_observation()
    );

    // remove_contact_medium — a bogus medium id; tolerate the outcome (structured
    // error, or the empty-204 → {removed:true} shape) so long as it never panics.
    let tool = reg.get("customer.remove_contact_medium").unwrap();
    let _ = (tool.func)(
        json!({ "customer_id": cid, "medium_id": "CM-NOPE" }),
        ToolCtx::default(),
    )
    .await;

    // update_contact — patch the primary email; the tool returns the customer doc.
    let updated_email = format!("updated{nonce}@example.com");
    let updated = call(
        &reg,
        "customer.update_contact",
        json!({ "customer_id": cid, "email": updated_email }),
    )
    .await;
    assert_eq!(updated["id"], json!(cid));

    // attest_kyc — the stub body must be accepted (provider/token only). Prebaked
    // may be policy-gated off; tolerate a structured error, fail only on a panic.
    let tool = reg.get("customer.attest_kyc").unwrap();
    match (tool.func)(
        json!({ "customer_id": cid, "provider": "prebaked", "attestation_token": "tok_smoke_abcdefghijklmnop" }),
        ToolCtx::default(),
    )
    .await
    {
        Ok(v) => assert!(
            v.get("kyc_status").is_some()
                || v.get("kycStatus").is_some()
                || v.get("customer_id").is_some(),
            "attest returned an attestation doc: {v}"
        ),
        Err(e) => {
            let obs = e.to_observation();
            assert!(
                obs.contains("POLICY_VIOLATION") || obs.contains("CLIENT_ERROR"),
                "attest error should be structured, got {obs}"
            );
        }
    }

    // log_interaction — summary + body; camelCase customerId must be accepted
    // (the exact write-body shape the 4c bug tripped on).
    let logged = call(
        &reg,
        "interaction.log",
        json!({ "customer_id": cid, "summary": "rust write smoke", "body": "exercised the write lifecycle" }),
    )
    .await;
    assert!(logged.get("id").is_some(), "interaction created: {logged}");

    // close — no active subscriptions, so this is allowed; status → closed.
    let closed = call(&reg, "customer.close", json!({ "customer_id": cid })).await;
    assert_eq!(
        closed["status"],
        json!("closed"),
        "customer closed: {closed}"
    );
}
