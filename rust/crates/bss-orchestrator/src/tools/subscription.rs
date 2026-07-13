//! Subscription read tools — the go-to diagnostic reads. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/subscription.py`.
//!
//! Every tool returns the `SubscriptionClient` response verbatim, so byte-parity
//! follows transitively from the P4 subscription service golden diff. Note
//! `subscription.get_esim_activation` is a **projected-dict** convenience — the
//! client reads the subscription and projects five fixed keys; its wire order
//! matches Python's dict-literal order via D9's `serde_json` `preserve_order`.
//!
//! The subscription *write* tools (terminate/renew/vas/plan-change) and the
//! ownership-bound `*.mine` chat wrappers land in later slices.

use std::sync::Arc;

use bss_clients::SubscriptionClient;
use futures_util::future::FutureExt;

use super::{map_client_err as map_err, req_str, RegisteredTool, ToolRegistry};

const DESC_GET: &str = include_str!("desc/subscription_get.txt");
const DESC_LIST_FOR_CUSTOMER: &str = include_str!("desc/subscription_list_for_customer.txt");
const DESC_GET_BALANCE: &str = include_str!("desc/subscription_get_balance.txt");
const DESC_GET_ESIM_ACTIVATION: &str = include_str!("desc/subscription_get_esim_activation.txt");

/// Register the four subscription **read** tools, each capturing a clone of `client`.
pub fn register_subscription_tools(registry: &mut ToolRegistry, client: SubscriptionClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.get".to_string(),
        description: DESC_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                c.get(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.list_for_customer".to_string(),
        description: DESC_LIST_FOR_CUSTOMER.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                c.list_for_customer(&cid).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.get_balance".to_string(),
        description: DESC_GET_BALANCE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                c.get_balance(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "subscription.get_esim_activation".to_string(),
        description: DESC_GET_ESIM_ACTIVATION.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                c.get_esim_activation(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}
