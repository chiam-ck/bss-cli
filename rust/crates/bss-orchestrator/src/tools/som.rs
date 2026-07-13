//! SOM read tools — ServiceOrder (TMF641) + service inventory (TMF638). Port of
//! the read slice of `orchestrator/bss_orchestrator/tools/som.py`. All verbatim
//! `SomClient` wrappers.

use std::sync::Arc;

use bss_clients::SomClient;
use futures_util::future::FutureExt;

use super::{map_client_err as map_err, req_str, RegisteredTool, ToolRegistry};

const DESC_SO_GET: &str = include_str!("desc/service_order_get.txt");
const DESC_SO_LIST: &str = include_str!("desc/service_order_list_for_order.txt");
const DESC_SVC_GET: &str = include_str!("desc/service_get.txt");
const DESC_SVC_LIST: &str = include_str!("desc/service_list_for_subscription.txt");

/// Register the four SOM **read** tools, each capturing a clone of `client`.
pub fn register_som_tools(registry: &mut ToolRegistry, client: SomClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "service_order.get".to_string(),
        description: DESC_SO_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "service_order_id")?;
                c.get_service_order(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "service_order.list_for_order".to_string(),
        description: DESC_SO_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "commercial_order_id")?;
                c.list_for_order(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "service.get".to_string(),
        description: DESC_SVC_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "service_id")?;
                c.get_service(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "service.list_for_subscription".to_string(),
        description: DESC_SVC_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                c.list_services_for_subscription(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}
