//! Case read tools — the CRM Case aggregate + linked chat transcript. Port of the
//! read slice of `orchestrator/bss_orchestrator/tools/case.py`.
//!
//! `case.get`/`case.list` are verbatim `CrmClient` wrappers. `case.show_transcript_for`
//! is a small composite: read the case, and if it carries a `chatTranscriptHash`,
//! fetch the stored transcript — otherwise return the `no_transcript_linked` sentinel
//! (key order preserved via D9). Case writes (open/close/note/transition/priority)
//! land with the CRM write slice.

use std::sync::Arc;

use bss_clients::CrmClient;
use futures_util::future::FutureExt;
use serde_json::{json, Value};

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_GET: &str = include_str!("desc/case_get.txt");
const DESC_LIST: &str = include_str!("desc/case_list.txt");
const DESC_SHOW_TRANSCRIPT: &str = include_str!("desc/case_show_transcript_for.txt");

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
