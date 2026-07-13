//! MNP port-request read tools (v0.17). Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/port_request.py`. Verbatim `CrmClient`
//! wrappers. The **write** tools (`register_port_request_write_tools`:
//! create/approve/reject) live here too. Port-request writes are operator-only
//! (never `customer_self_serve` — v0.17 doctrine).

use std::sync::Arc;

use bss_clients::CrmClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_LIST: &str = include_str!("desc/port_request_list.txt");
const DESC_GET: &str = include_str!("desc/port_request_get.txt");
const DESC_CREATE: &str = include_str!("desc/port_request_create.txt");
const DESC_APPROVE: &str = include_str!("desc/port_request_approve.txt");
const DESC_REJECT: &str = include_str!("desc/port_request_reject.txt");

/// Register the two port-request **read** tools, each capturing a clone of `client`.
pub fn register_port_request_tools(registry: &mut ToolRegistry, client: CrmClient) {
    // port_request.list — state/direction optional; limit 50 / offset 0 defaults.
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "port_request.list".to_string(),
        description: DESC_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let state = opt_str(&args, "state");
                let direction = opt_str(&args, "direction");
                let limit = args.get("limit").and_then(Value::as_i64).unwrap_or(50);
                let offset = args.get("offset").and_then(Value::as_i64).unwrap_or(0);
                c.list_port_requests(state.as_deref(), direction.as_deref(), limit, offset)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "port_request.get".to_string(),
        description: DESC_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "port_request_id")?;
                c.get_port_request(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}

/// Register the three port-request **write** tools, each capturing a clone of
/// `client`. All operator-only (never customer_self_serve).
pub fn register_port_request_write_tools(registry: &mut ToolRegistry, client: CrmClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "port_request.create".to_string(),
        description: DESC_CREATE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let direction = req_str(&args, "direction")?;
                let donor_carrier = req_str(&args, "donor_carrier")?;
                let donor_msisdn = req_str(&args, "donor_msisdn")?;
                let requested_port_date = req_str(&args, "requested_port_date")?;
                let target = opt_str(&args, "target_subscription_id");
                c.create_port_request(
                    &direction,
                    &donor_carrier,
                    &donor_msisdn,
                    &requested_port_date,
                    target.as_deref(),
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "port_request.approve".to_string(),
        description: DESC_APPROVE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "port_request_id")?;
                c.approve_port_request(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "port_request.reject".to_string(),
        description: DESC_REJECT.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "port_request_id")?;
                let reason = req_str(&args, "reason")?;
                c.reject_port_request(&id, &reason).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}
