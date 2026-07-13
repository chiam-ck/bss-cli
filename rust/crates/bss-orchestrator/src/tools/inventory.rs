//! Inventory read tools — MSISDN + eSIM pools (hosted on CRM). Port of the read
//! slice of `orchestrator/bss_orchestrator/tools/inventory.py`. All verbatim
//! `InventoryClient` wrappers. The `msisdn.add_range` write lands with the
//! inventory-write slice.

use std::sync::Arc;

use bss_clients::InventoryClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_MSISDN_LIST: &str = include_str!("desc/inventory_msisdn_list_available.txt");
const DESC_MSISDN_GET: &str = include_str!("desc/inventory_msisdn_get.txt");
const DESC_MSISDN_COUNT: &str = include_str!("desc/inventory_msisdn_count.txt");
const DESC_ESIM_LIST: &str = include_str!("desc/inventory_esim_list_available.txt");
const DESC_ESIM_ACTIVATION: &str = include_str!("desc/inventory_esim_get_activation.txt");

/// Register the five inventory **read** tools, each capturing a clone of `client`.
pub fn register_inventory_tools(registry: &mut ToolRegistry, client: InventoryClient) {
    // inventory.msisdn.list_available — `status` defaults to "available" when the
    // key is ABSENT, but an explicit `null` means "any state" (Python's
    // `status: str | None = "available"`). `opt_str` collapses both, so decode the
    // three cases by hand.
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "inventory.msisdn.list_available".to_string(),
        description: DESC_MSISDN_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let status: Option<String> = match args.get("status") {
                    None => Some("available".to_string()),
                    Some(Value::Null) => None,
                    Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
                    _ => Some("available".to_string()),
                };
                let prefix = opt_str(&args, "prefix");
                let limit = args.get("limit").and_then(Value::as_i64).unwrap_or(20);
                c.list_msisdns(status.as_deref(), prefix.as_deref(), limit)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "inventory.msisdn.get".to_string(),
        description: DESC_MSISDN_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let msisdn = req_str(&args, "msisdn")?;
                c.get_msisdn(&msisdn).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "inventory.msisdn.count".to_string(),
        description: DESC_MSISDN_COUNT.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let prefix = opt_str(&args, "prefix");
                c.count_msisdns(prefix.as_deref()).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // inventory.esim.list_available — state is fixed to "available" (the Python
    // tool hardcodes it); only `limit` is caller-controlled.
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "inventory.esim.list_available".to_string(),
        description: DESC_ESIM_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let limit = args.get("limit").and_then(Value::as_i64).unwrap_or(20);
                c.list_esims(Some("available"), limit)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "inventory.esim.get_activation".to_string(),
        description: DESC_ESIM_ACTIVATION.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let iccid = req_str(&args, "iccid")?;
                c.get_activation_code(&iccid).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}
