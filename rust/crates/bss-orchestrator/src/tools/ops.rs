//! Ops read tools — `events.list` (v0.1 NOT_IMPLEMENTED stub) + `agents.list`
//! (CRM). Port of the non-clock, non-trace slice of
//! `orchestrator/bss_orchestrator/tools/ops.py`. The trace tools
//! (`trace.get`/`for_order`/`for_subscription`) need a Jaeger client + audit-event
//! resolution and land in their own slice.

use std::sync::Arc;

use bss_clients::CrmClient;
use futures_util::future::FutureExt;
use serde_json::{json, Value};

use super::{map_client_err as map_err, opt_str, RegisteredTool, ToolRegistry};

const DESC_EVENTS_LIST: &str = include_str!("desc/events_list.txt");
const DESC_AGENTS_LIST: &str = include_str!("desc/agents_list.txt");

/// The v0.1 event-bus stub message, embedded byte-for-byte from Python's
/// `_EVENTS_NOT_IMPLEMENTED` (R2 — the LLM sees this observation verbatim).
const EVENTS_MESSAGE: &str = "Event-bus query tools ship in Phase 11. Events are already persisted to audit.domain_event in every service schema; query them via SQL for now.";

/// Register the ops read tools. `events.list` is client-free (a stub); `agents.list`
/// captures a clone of `crm`.
pub fn register_ops_tools(registry: &mut ToolRegistry, crm: CrmClient) {
    // events.list — NOT_IMPLEMENTED in v0.1. Echoes the filter args back after the
    // base error/message (key order preserved via D9's preserve_order).
    registry.register(RegisteredTool {
        name: "events.list".to_string(),
        description: DESC_EVENTS_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            async move {
                Ok(json!({
                    "error": "NOT_IMPLEMENTED",
                    "message": EVENTS_MESSAGE,
                    "aggregateType": args.get("aggregate_type").cloned().unwrap_or(Value::Null),
                    "aggregateId": args.get("aggregate_id").cloned().unwrap_or(Value::Null),
                    "since": args.get("since").cloned().unwrap_or(Value::Null),
                    "limit": args.get("limit").cloned().unwrap_or(json!(50)),
                }))
            }
            .boxed()
        }),
    });

    let c = crm;
    registry.register(RegisteredTool {
        name: "agents.list".to_string(),
        description: DESC_AGENTS_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let state = opt_str(&args, "state");
                c.list_agents(state.as_deref()).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}
