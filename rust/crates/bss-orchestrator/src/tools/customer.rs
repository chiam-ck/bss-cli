//! CRM read tools — customer lookup + the `customer.get` 360 composite +
//! interaction log. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/customer.py`.
//!
//! Every tool but `customer.get` returns the `CrmClient` response verbatim, so
//! byte-parity follows transitively from the P4 CRM service golden diff. The
//! `customer.get` composite fans four independent reads out in parallel (CRM
//! customer + cases + interactions, Subscription line list) and stitches the
//! adjacent context under the synthetic `_extras` key the cockpit's 360 renderer
//! expects — mirroring the Python tool's `asyncio.gather(..., return_exceptions=
//! True)` exactly: the customer read is the hard error; the three sub-reads
//! degrade to empty lists so a downstream outage never wedges the 360.
//!
//! The customer *write* tools (create/update/close/attest_kyc, interaction.log)
//! land with the CRM write-client methods in a later slice.

use std::sync::Arc;

use bss_clients::{ClientError, CrmClient, SubscriptionClient};
use futures_util::future::{join4, FutureExt};
use serde_json::{json, Value};

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_GET: &str = include_str!("desc/customer_get.txt");
const DESC_LIST: &str = include_str!("desc/customer_list.txt");
const DESC_FIND_BY_MSISDN: &str = include_str!("desc/customer_find_by_msisdn.txt");
const DESC_FIND_BY_EMAIL: &str = include_str!("desc/customer_find_by_email.txt");
const DESC_GET_KYC_STATUS: &str = include_str!("desc/customer_get_kyc_status.txt");
const DESC_INTERACTION_LIST: &str = include_str!("desc/interaction_list.txt");

/// A sub-read result → a JSON array, degrading any error (or non-array success)
/// to `[]` — the Rust shape of Python's `x if isinstance(x, list) else []`.
fn ok_array(r: Result<Value, ClientError>) -> Value {
    match r {
        Ok(v) if v.is_array() => v,
        _ => Value::Array(Vec::new()),
    }
}

/// Register the CRM read family. `crm` backs every tool; `subscription` is needed
/// only by the `customer.get` composite's line-list fan-out.
pub fn register_customer_tools(
    registry: &mut ToolRegistry,
    crm: CrmClient,
    subscription: SubscriptionClient,
) {
    // customer.get — the 360 composite (crm + subscription, four parallel reads).
    let c = crm.clone();
    let s = subscription.clone();
    registry.register(RegisteredTool {
        name: "customer.get".to_string(),
        description: DESC_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            let s = s.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                // Four independent reads, bounded by the slowest — not their sum.
                let (customer, subs, cases, interactions) = join4(
                    c.get_customer(&cid),
                    s.list_for_customer(&cid),
                    c.list_cases(Some(&cid), None),
                    c.list_interactions(&cid, 10),
                )
                .await;
                // The customer record itself is the primary read — its failure is
                // a hard error (a real NotFound the caller must see).
                let mut customer = customer.map_err(map_err)?;
                let extras = json!({
                    "subscriptions": ok_array(subs),
                    "cases": ok_array(cases),
                    "interactions": ok_array(interactions),
                });
                if let Some(obj) = customer.as_object_mut() {
                    obj.insert("_extras".to_string(), extras);
                }
                Ok(customer)
            }
            .boxed()
        }),
    });

    // customer.list — verbatim, optional state/name filters.
    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.list".to_string(),
        description: DESC_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let state = opt_str(&args, "state");
                let name = opt_str(&args, "name_contains");
                c.list_customers(state.as_deref(), name.as_deref())
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    // customer.find_by_msisdn — verbatim.
    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.find_by_msisdn".to_string(),
        description: DESC_FIND_BY_MSISDN.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let msisdn = req_str(&args, "msisdn")?;
                c.find_customer_by_msisdn(&msisdn).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // customer.find_by_email — verbatim.
    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.find_by_email".to_string(),
        description: DESC_FIND_BY_EMAIL.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let email = req_str(&args, "email")?;
                c.find_customer_by_email(&email).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // customer.get_kyc_status — verbatim.
    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.get_kyc_status".to_string(),
        description: DESC_GET_KYC_STATUS.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                c.get_kyc_status(&cid).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // interaction.list — verbatim, limit defaults to 50 (Python default).
    let c = crm;
    registry.register(RegisteredTool {
        name: "interaction.list".to_string(),
        description: DESC_INTERACTION_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                let limit = args.get("limit").and_then(Value::as_i64).unwrap_or(50);
                c.list_interactions(&cid, limit).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}
