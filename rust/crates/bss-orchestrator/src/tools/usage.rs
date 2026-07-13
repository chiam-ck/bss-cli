//! Usage read tool — TMF635 usage events via Mediation. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/usage.py`. Verbatim `MediationClient`
//! wrapper. The `usage.simulate` write (LLM-hidden) lands with the mediation-write
//! slice; the `usage.history_mine` chat wrapper lands with the customer_self_serve
//! slice.

use std::sync::Arc;

use bss_clients::MediationClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, RegisteredTool, ToolRegistry};

const DESC_HISTORY: &str = include_str!("desc/usage_history.txt");

/// Register the `usage.history` read tool, capturing a clone of `client`.
pub fn register_usage_tools(registry: &mut ToolRegistry, client: MediationClient) {
    let c = client;
    registry.register(RegisteredTool {
        name: "usage.history".to_string(),
        description: DESC_HISTORY.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let subscription_id = opt_str(&args, "subscription_id");
                let msisdn = opt_str(&args, "msisdn");
                let event_type = opt_str(&args, "event_type");
                let since = opt_str(&args, "since");
                let limit = args.get("limit").and_then(Value::as_i64).unwrap_or(100);
                c.list_usage(
                    subscription_id.as_deref(),
                    msisdn.as_deref(),
                    event_type.as_deref(),
                    since.as_deref(),
                    limit,
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });
}
