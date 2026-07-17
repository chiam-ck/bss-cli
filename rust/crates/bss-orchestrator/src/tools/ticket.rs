//! Trouble-ticket tools — TMF621. Port of `orchestrator/bss_orchestrator/tools/
//! ticket.py`. Reads are verbatim `CrmClient` wrappers; the **write** tools
//! (`register_ticket_write_tools`) map a friendly target `state` → the state-machine
//! `trigger` the API takes. `in_progress` is reachable via three triggers
//! (start/resume/reopen), so that target costs one `get_ticket` read to resolve;
//! an unknown target/source yields a `ValueError` observation (matching Python).

use std::sync::Arc;

use bss_clients::CrmClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{
    map_client_err as map_err, opt_str, py_list_repr, req_str, RegisteredTool, ToolError,
    ToolRegistry,
};

const DESC_GET: &str = include_str!("desc/ticket_get.txt");
const DESC_LIST: &str = include_str!("desc/ticket_list.txt");
const DESC_OPEN: &str = include_str!("desc/ticket_open.txt");
const DESC_ASSIGN: &str = include_str!("desc/ticket_assign.txt");
const DESC_TRANSITION: &str = include_str!("desc/ticket_transition.txt");
const DESC_RESOLVE: &str = include_str!("desc/ticket_resolve.txt");
const DESC_CLOSE: &str = include_str!("desc/ticket_close.txt");
const DESC_CANCEL: &str = include_str!("desc/ticket_cancel.txt");

// The FSM maps live on `CrmClient` — the cockpit's CRM workbench needs the same
// tables, and Python keeps them in the client for exactly that reason.
use bss_clients::{
    ticket_in_progress_trigger, ticket_trigger_for_state, TICKET_IN_PROGRESS_BY_SOURCE,
    TICKET_STATE_TO_TRIGGER,
};

fn value_error(detail: String) -> ToolError {
    ToolError::Other {
        kind: "ValueError".to_string(),
        detail,
    }
}

/// Register the two ticket **read** tools, each capturing a clone of `client`.
pub fn register_ticket_tools(registry: &mut ToolRegistry, client: CrmClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "ticket.get".to_string(),
        description: DESC_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "ticket_id")?;
                c.get_ticket(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "ticket.list".to_string(),
        description: DESC_LIST.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let customer_id = opt_str(&args, "customer_id");
                let case_id = opt_str(&args, "case_id");
                let state = opt_str(&args, "state");
                let agent_id = opt_str(&args, "agent_id");
                c.list_tickets(
                    customer_id.as_deref(),
                    case_id.as_deref(),
                    state.as_deref(),
                    agent_id.as_deref(),
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });
}

/// Resolve a target state → trigger, reading the ticket's current state when the
/// target is `in_progress` (three triggers land there). Mirrors the Python client's
/// `transition_ticket` mapping + `ValueError`s exactly.
async fn resolve_ticket_trigger(
    c: &CrmClient,
    ticket_id: &str,
    to_state: &str,
) -> Result<String, ToolError> {
    if to_state == "in_progress" {
        let current = c.get_ticket(ticket_id).await.map_err(map_err)?;
        let src = current.get("state").and_then(Value::as_str).unwrap_or("");
        ticket_in_progress_trigger(src)
            .map(str::to_string)
            .ok_or_else(|| {
                let sources: Vec<&str> = TICKET_IN_PROGRESS_BY_SOURCE
                    .iter()
                    .map(|(s, _)| *s)
                    .collect();
                value_error(format!(
                    "No transition to in_progress from '{src}'; valid sources: {}",
                    py_list_repr(&sources)
                ))
            })
    } else {
        ticket_trigger_for_state(to_state)
            .map(str::to_string)
            .ok_or_else(|| {
                let targets: Vec<&str> = TICKET_STATE_TO_TRIGGER.iter().map(|(s, _)| *s).collect();
                value_error(format!(
                    "Unknown target state '{to_state}'; valid targets: {} + ['in_progress']",
                    py_list_repr(&targets)
                ))
            })
    }
}

/// Register the six ticket **write** tools, each capturing a clone of `client`.
/// `ticket.cancel` is destructive (safety-gated at the tool boundary).
pub fn register_ticket_write_tools(registry: &mut ToolRegistry, client: CrmClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "ticket.open".to_string(),
        description: DESC_OPEN.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let ticket_type = req_str(&args, "ticket_type")?;
                let subject = req_str(&args, "subject")?;
                let case_id = opt_str(&args, "case_id");
                let customer_id = opt_str(&args, "customer_id");
                let order_id = opt_str(&args, "order_id");
                let subscription_id = opt_str(&args, "subscription_id");
                let service_id = opt_str(&args, "service_id");
                c.open_ticket(
                    &ticket_type,
                    &subject,
                    case_id.as_deref(),
                    customer_id.as_deref(),
                    order_id.as_deref(),
                    subscription_id.as_deref(),
                    service_id.as_deref(),
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "ticket.assign".to_string(),
        description: DESC_ASSIGN.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let ticket_id = req_str(&args, "ticket_id")?;
                let agent_id = req_str(&args, "agent_id")?;
                c.assign_ticket(&ticket_id, &agent_id)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "ticket.transition".to_string(),
        description: DESC_TRANSITION.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let ticket_id = req_str(&args, "ticket_id")?;
                let to_state = req_str(&args, "to_state")?;
                let trigger = resolve_ticket_trigger(&c, &ticket_id, &to_state).await?;
                c.transition_ticket(&ticket_id, &trigger)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "ticket.resolve".to_string(),
        description: DESC_RESOLVE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let ticket_id = req_str(&args, "ticket_id")?;
                let resolution_notes = req_str(&args, "resolution_notes")?;
                c.resolve_ticket(&ticket_id, &resolution_notes)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    // ticket.close — transition to `closed` (→ trigger `close`), mirroring the
    // Python client's `close_ticket` delegating to `transition_ticket`.
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "ticket.close".to_string(),
        description: DESC_CLOSE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let ticket_id = req_str(&args, "ticket_id")?;
                let trigger = resolve_ticket_trigger(&c, &ticket_id, "closed").await?;
                c.transition_ticket(&ticket_id, &trigger)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "ticket.cancel".to_string(),
        description: DESC_CANCEL.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let ticket_id = req_str(&args, "ticket_id")?;
                c.cancel_ticket(&ticket_id).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}
