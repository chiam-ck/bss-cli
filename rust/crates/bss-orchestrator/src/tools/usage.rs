//! Usage read tool — TMF635 usage events via Mediation. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/usage.py`. Verbatim `MediationClient`
//! wrapper. The `usage.simulate` write (LLM-hidden, `register_usage_write_tools`)
//! lives here too; the `usage.history_mine` chat wrapper lands with the
//! customer_self_serve slice.

use std::sync::Arc;

use bss_clients::MediationClient;
use chrono::Timelike;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_HISTORY: &str = include_str!("desc/usage_history.txt");
const DESC_SIMULATE: &str = include_str!("desc/usage_simulate.txt");

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

/// Register the `usage.simulate` write tool (LLM-hidden), capturing a clone of
/// `client`. `event_time` defaults to whole-second now (`clock_now().replace(
/// microsecond=0).isoformat()`), matching the Python tool.
pub fn register_usage_write_tools(registry: &mut ToolRegistry, client: MediationClient) {
    let c = client;
    registry.register(RegisteredTool {
        name: "usage.simulate".to_string(),
        description: DESC_SIMULATE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let msisdn = req_str(&args, "msisdn")?;
                let event_type = req_str(&args, "event_type")?;
                let quantity = args.get("quantity").and_then(Value::as_i64).unwrap_or(0);
                let unit = req_str(&args, "unit")?;
                let roaming = args
                    .get("roaming_indicator")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                let event_time = opt_str(&args, "event_time").unwrap_or_else(|| {
                    let now = bss_clock::now()
                        .with_nanosecond(0)
                        .unwrap_or_else(bss_clock::now);
                    bss_clock::isoformat(now)
                });
                c.submit_usage(&msisdn, &event_type, &event_time, quantity, &unit, roaming)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });
}
