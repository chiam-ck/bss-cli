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
//! The customer + interaction **write** tools (`register_customer_write_tools`)
//! live alongside the reads here, mirroring the single Python `customer.py` module.

use std::sync::Arc;

use bss_clients::{ClientError, CrmClient, SubscriptionClient};
use futures_util::future::{join4, FutureExt};
use serde_json::{json, Map, Value};

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_GET: &str = include_str!("desc/customer_get.txt");
const DESC_LIST: &str = include_str!("desc/customer_list.txt");
const DESC_FIND_BY_MSISDN: &str = include_str!("desc/customer_find_by_msisdn.txt");
const DESC_FIND_BY_EMAIL: &str = include_str!("desc/customer_find_by_email.txt");
const DESC_GET_KYC_STATUS: &str = include_str!("desc/customer_get_kyc_status.txt");
const DESC_INTERACTION_LIST: &str = include_str!("desc/interaction_list.txt");
const DESC_CREATE: &str = include_str!("desc/customer_create.txt");
const DESC_UPDATE_CONTACT: &str = include_str!("desc/customer_update_contact.txt");
const DESC_ADD_CONTACT_MEDIUM: &str = include_str!("desc/customer_add_contact_medium.txt");
const DESC_REMOVE_CONTACT_MEDIUM: &str = include_str!("desc/customer_remove_contact_medium.txt");
const DESC_ATTEST_KYC: &str = include_str!("desc/customer_attest_kyc.txt");
const DESC_CLOSE: &str = include_str!("desc/customer_close.txt");
const DESC_INTERACTION_LOG: &str = include_str!("desc/interaction_log.txt");

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
                    c.list_cases(Some(&cid), None, None),
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

/// Register the customer + interaction **write** tools, each capturing a clone of
/// `crm`. `customer.remove_contact_medium` and `customer.close` are in
/// `DESTRUCTIVE_TOOLS` — the safety layer gates them; the tool itself just writes.
pub fn register_customer_write_tools(registry: &mut ToolRegistry, crm: CrmClient) {
    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.create".to_string(),
        description: DESC_CREATE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let name = req_str(&args, "name")?;
                let email = opt_str(&args, "email");
                let phone = opt_str(&args, "phone");
                c.create_customer(&name, email.as_deref(), phone.as_deref())
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    // customer.update_contact — patch only the supplied fields (empty patch is a
    // no-op PATCH, matching Python).
    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.update_contact".to_string(),
        description: DESC_UPDATE_CONTACT.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                let mut patch = Map::new();
                if let Some(e) = opt_str(&args, "email") {
                    patch.insert("email".to_string(), json!(e));
                }
                if let Some(p) = opt_str(&args, "phone") {
                    patch.insert("phone".to_string(), json!(p));
                }
                c.update_customer(&cid, &Value::Object(patch))
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.add_contact_medium".to_string(),
        description: DESC_ADD_CONTACT_MEDIUM.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                let medium_type = req_str(&args, "medium_type")?;
                let value = req_str(&args, "value")?;
                c.add_contact_medium(&cid, &medium_type, &value)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.remove_contact_medium".to_string(),
        description: DESC_REMOVE_CONTACT_MEDIUM.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                let medium_id = req_str(&args, "medium_id")?;
                c.remove_contact_medium(&cid, &medium_id)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.attest_kyc".to_string(),
        description: DESC_ATTEST_KYC.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                let provider = req_str(&args, "provider")?;
                let token = req_str(&args, "attestation_token")?;
                c.attest_kyc(&cid, &provider, &token).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = crm.clone();
    registry.register(RegisteredTool {
        name: "customer.close".to_string(),
        description: DESC_CLOSE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                c.close_customer(&cid).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = crm;
    registry.register(RegisteredTool {
        name: "interaction.log".to_string(),
        description: DESC_INTERACTION_LOG.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                let summary = req_str(&args, "summary")?;
                let body = opt_str(&args, "body");
                c.log_interaction(&cid, &summary, body.as_deref())
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });
}
