//! Provisioning read tools — the provisioning simulator's task surface. Port of
//! the read slice of `orchestrator/bss_orchestrator/tools/provisioning.py`. Both
//! verbatim `ProvisioningClient` wrappers. The resolve/retry/fault-injection writes
//! land with the provisioning-write slice.

use std::sync::Arc;

use bss_clients::ProvisioningClient;
use futures_util::future::FutureExt;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_GET_TASK: &str = include_str!("desc/provisioning_get_task.txt");
const DESC_LIST_TASKS: &str = include_str!("desc/provisioning_list_tasks.txt");

/// Register the two provisioning **read** tools, each capturing a clone of `client`.
pub fn register_provisioning_tools(registry: &mut ToolRegistry, client: ProvisioningClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "provisioning.get_task".to_string(),
        description: DESC_GET_TASK.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "task_id")?;
                c.get_task(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "provisioning.list_tasks".to_string(),
        description: DESC_LIST_TASKS.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let service_id = opt_str(&args, "service_id");
                let state = opt_str(&args, "state");
                c.list_tasks(service_id.as_deref(), state.as_deref())
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });
}
