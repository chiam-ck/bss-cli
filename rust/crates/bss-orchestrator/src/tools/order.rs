//! Commercial Order read tools — TMF622 productOrder. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/order.py`.
//!
//! `order.get`/`order.list` are verbatim `ComClient` wrappers. `order.wait_until`
//! is a **polling composite**: it loops `get_order` until the target (or a terminal
//! `failed`/`cancelled`) state, or times out — a `ClientError::Timeout` maps to the
//! 504-shaped observation, matching the Python client's `Timeout`. The order
//! **write** tools (`register_order_write_tools`) live here too — `order.create` is
//! the create+submit composite.

use std::sync::Arc;

use bss_clients::ComClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_GET: &str = include_str!("desc/order_get.txt");
const DESC_LIST: &str = include_str!("desc/order_list.txt");
const DESC_WAIT_UNTIL: &str = include_str!("desc/order_wait_until.txt");
const DESC_CREATE: &str = include_str!("desc/order_create.txt");
const DESC_CANCEL: &str = include_str!("desc/order_cancel.txt");

/// Register the three order **read** tools, each capturing a clone of `client`.
pub fn register_order_tools(registry: &mut ToolRegistry, client: ComClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "order.get".to_string(),
        description: DESC_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "order_id")?;
                c.get_order(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "order.list".to_string(),
        description: DESC_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                c.list_orders(Some(&cid)).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // order.wait_until — target_state defaults "completed", timeout_s 30.0, and the
    // client's fixed 0.5s poll interval (matching the Python client default).
    let c = client;
    registry.register(RegisteredTool {
        name: "order.wait_until".to_string(),
        description: DESC_WAIT_UNTIL.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "order_id")?;
                let target =
                    opt_str(&args, "target_state").unwrap_or_else(|| "completed".to_string());
                let timeout_s = args
                    .get("timeout_s")
                    .and_then(Value::as_f64)
                    .unwrap_or(30.0);
                c.wait_until(&id, &target, timeout_s, 0.5)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });
}

/// Register the two order **write** tools, each capturing a clone of `client`.
/// `order.cancel` is destructive (safety-gated at the tool boundary).
pub fn register_order_write_tools(registry: &mut ToolRegistry, client: ComClient) {
    // order.create — create THEN submit (the Python tool's composite). Both halves
    // must succeed; the submit response (state `acknowledged`) is returned.
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "order.create".to_string(),
        description: DESC_CREATE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let customer_id = req_str(&args, "customer_id")?;
                let offering_id = req_str(&args, "offering_id")?;
                let msisdn_preference = opt_str(&args, "msisdn_preference");
                let notes = opt_str(&args, "notes");
                let discount_code = opt_str(&args, "discount_code");
                let order = c
                    .create_order(
                        &customer_id,
                        &offering_id,
                        msisdn_preference.as_deref(),
                        notes.as_deref(),
                        discount_code.as_deref(),
                    )
                    .await
                    .map_err(map_err)?;
                let order_id = order.get("id").and_then(Value::as_str).ok_or_else(|| {
                    super::ToolError::Other {
                        kind: "KeyError".to_string(),
                        detail: "created order has no id".to_string(),
                    }
                })?;
                c.submit_order(order_id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "order.cancel".to_string(),
        description: DESC_CANCEL.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let order_id = req_str(&args, "order_id")?;
                c.cancel_order(&order_id).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}
