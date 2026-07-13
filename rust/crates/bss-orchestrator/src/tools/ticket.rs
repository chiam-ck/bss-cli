//! Trouble-ticket read tools — TMF621. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/ticket.py`. Verbatim `CrmClient` wrappers.
//! Ticket writes (open/assign/transition/resolve/close/cancel) land with the CRM
//! write slice.

use std::sync::Arc;

use bss_clients::CrmClient;
use futures_util::future::FutureExt;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_GET: &str = include_str!("desc/ticket_get.txt");
const DESC_LIST: &str = include_str!("desc/ticket_list.txt");

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
