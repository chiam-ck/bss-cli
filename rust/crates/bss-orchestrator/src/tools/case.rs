//! Case read tools — the CRM Case aggregate + linked chat transcript. Port of the
//! read slice of `orchestrator/bss_orchestrator/tools/case.py`.
//!
//! `case.get`/`case.list` are verbatim `CrmClient` wrappers. `case.show_transcript_for`
//! is a small composite: read the case, and if it carries a `chatTranscriptHash`,
//! fetch the stored transcript — otherwise return the `no_transcript_linked` sentinel
//! (key order preserved via D9). The case **write** tools
//! (`register_case_write_tools`) live here too.
//!
//! `case.transition` maps a friendly target `state` → the state-machine `trigger`
//! the API takes (the CRM route binds `{"trigger": …}`, not `{"state": …}`);
//! an unknown target yields a `ValueError` observation (matching Python's
//! `transition_case` `raise ValueError`, which the graph renders as
//! `{"error":"ValueError", …}`).

use std::sync::Arc;

use bss_clients::CrmClient;
use futures_util::future::FutureExt;
use serde_json::{json, Value};

use super::{
    map_client_err as map_err, opt_str, py_list_repr, req_str, RegisteredTool, ToolError,
    ToolRegistry,
};

const DESC_GET: &str = include_str!("desc/case_get.txt");
const DESC_LIST: &str = include_str!("desc/case_list.txt");
const DESC_SHOW_TRANSCRIPT: &str = include_str!("desc/case_show_transcript_for.txt");
const DESC_OPEN: &str = include_str!("desc/case_open.txt");
const DESC_CLOSE: &str = include_str!("desc/case_close.txt");
const DESC_ADD_NOTE: &str = include_str!("desc/case_add_note.txt");
const DESC_TRANSITION: &str = include_str!("desc/case_transition.txt");
const DESC_UPDATE_PRIORITY: &str = include_str!("desc/case_update_priority.txt");

/// Target case state → state-machine trigger (`_STATE_TO_TRIGGER` in the Python
/// client). Sorted for the "valid targets" error message.
const CASE_STATE_TO_TRIGGER: &[(&str, &str)] = &[
    ("closed", "close"),
    ("in_progress", "take"),
    ("pending_customer", "await_customer"),
    ("resolved", "resolve"),
];

fn case_trigger(to_state: &str) -> Result<&'static str, ToolError> {
    CASE_STATE_TO_TRIGGER
        .iter()
        .find(|(s, _)| *s == to_state)
        .map(|(_, t)| *t)
        .ok_or_else(|| {
            let valid: Vec<&str> = CASE_STATE_TO_TRIGGER.iter().map(|(s, _)| *s).collect();
            ToolError::Other {
                kind: "ValueError".to_string(),
                detail: format!(
                    "Unknown target state '{to_state}'; valid targets: {}",
                    py_list_repr(&valid)
                ),
            }
        })
}

/// Register the three case **read** tools, each capturing a clone of `client`.
pub fn register_case_tools(registry: &mut ToolRegistry, client: CrmClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "case.get".to_string(),
        description: DESC_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "case_id")?;
                c.get_case(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "case.list".to_string(),
        description: DESC_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let customer_id = opt_str(&args, "customer_id");
                let state = opt_str(&args, "state");
                let agent_id = opt_str(&args, "agent_id");
                c.list_cases(
                    customer_id.as_deref(),
                    state.as_deref(),
                    agent_id.as_deref(),
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });

    // case.show_transcript_for — composite: case → hash → transcript, else sentinel.
    let c = client;
    registry.register(RegisteredTool {
        name: "case.show_transcript_for".to_string(),
        description: DESC_SHOW_TRANSCRIPT.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "case_id")?;
                let case = c.get_case(&id).await.map_err(map_err)?;
                let hash = case
                    .get("chatTranscriptHash")
                    .or_else(|| case.get("chat_transcript_hash"))
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty());
                match hash {
                    None => Ok(json!({ "transcript": null, "reason": "no_transcript_linked" })),
                    Some(h) => c.get_chat_transcript(h).await.map_err(map_err),
                }
            }
            .boxed()
        }),
    });
}

/// Register the five case **write** tools, each capturing a clone of `client`.
/// `case.close` is destructive (safety-gated at the tool boundary).
pub fn register_case_write_tools(registry: &mut ToolRegistry, client: CrmClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "case.open".to_string(),
        description: DESC_OPEN.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let customer_id = req_str(&args, "customer_id")?;
                let subject = req_str(&args, "subject")?;
                let category = req_str(&args, "category")?;
                let priority = req_str(&args, "priority")?;
                c.open_case(
                    &customer_id,
                    &subject,
                    &category,
                    &priority,
                    None,
                    None,
                    None,
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "case.add_note".to_string(),
        description: DESC_ADD_NOTE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let case_id = req_str(&args, "case_id")?;
                let body = req_str(&args, "body")?;
                c.add_case_note(&case_id, &body).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "case.update_priority".to_string(),
        description: DESC_UPDATE_PRIORITY.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let case_id = req_str(&args, "case_id")?;
                let priority = req_str(&args, "priority")?;
                c.patch_case(&case_id, &json!({"priority": priority}))
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    // case.transition — map target state → trigger; the API takes {"trigger"}.
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "case.transition".to_string(),
        description: DESC_TRANSITION.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let case_id = req_str(&args, "case_id")?;
                let to_state = req_str(&args, "to_state")?;
                let trigger = case_trigger(&to_state)?;
                c.patch_case(&case_id, &json!({"trigger": trigger}))
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "case.close".to_string(),
        description: DESC_CLOSE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let case_id = req_str(&args, "case_id")?;
                let resolution_code = req_str(&args, "resolution_code")?;
                c.close_case(&case_id, &resolution_code)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });
}
