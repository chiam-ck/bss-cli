//! Commercial Order read tools — TMF622 productOrder. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/order.py`.
//!
//! `order.get`/`order.list` are verbatim `ComClient` wrappers. `order.wait_until`
//! is a **polling composite**: it loops `get_order` until the target (or a terminal
//! `failed`/`cancelled`) state, or times out — a `ClientError::Timeout` maps to the
//! 504-shaped observation, matching the Python client's `Timeout`. Order writes
//! (`create`/`cancel`) land with the order-write slice.

use std::sync::Arc;

use bss_clients::ComClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_GET: &str = include_str!("desc/order_get.txt");
const DESC_LIST: &str = include_str!("desc/order_list.txt");
const DESC_WAIT_UNTIL: &str = include_str!("desc/order_wait_until.txt");

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
