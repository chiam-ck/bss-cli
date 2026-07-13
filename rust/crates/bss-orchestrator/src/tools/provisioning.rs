//! Provisioning read tools — the provisioning simulator's task surface. Port of
//! the read slice of `orchestrator/bss_orchestrator/tools/provisioning.py`. Both
//! verbatim `ProvisioningClient` wrappers. The **write** tools
//! (`register_provisioning_write_tools`) live here too — `set_fault_injection` is a
//! list→find→patch composite (returns a `NOT_FOUND` sentinel when no injector matches
//! the `(task_type, fault_type)` pair, matching Python).

use std::sync::Arc;

use bss_clients::ProvisioningClient;
use futures_util::future::FutureExt;
use serde_json::{json, Value};

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_GET_TASK: &str = include_str!("desc/provisioning_get_task.txt");
const DESC_LIST_TASKS: &str = include_str!("desc/provisioning_list_tasks.txt");
const DESC_RESOLVE_STUCK: &str = include_str!("desc/provisioning_resolve_stuck.txt");
const DESC_RETRY_FAILED: &str = include_str!("desc/provisioning_retry_failed.txt");
const DESC_SET_FAULT: &str = include_str!("desc/provisioning_set_fault_injection.txt");

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

/// Register the three provisioning **write** tools, each capturing a clone of
/// `client`. `set_fault_injection` is destructive (safety-gated at the tool).
pub fn register_provisioning_write_tools(registry: &mut ToolRegistry, client: ProvisioningClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "provisioning.resolve_stuck".to_string(),
        description: DESC_RESOLVE_STUCK.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let task_id = req_str(&args, "task_id")?;
                let note = req_str(&args, "note")?;
                c.resolve_task(&task_id, &note).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "provisioning.retry_failed".to_string(),
        description: DESC_RETRY_FAILED.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let task_id = req_str(&args, "task_id")?;
                c.retry_task(&task_id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // provisioning.set_fault_injection — list → find (task_type, fault_type) → patch,
    // or the NOT_FOUND sentinel when no injector matches (composite; matches Python).
    let c = client;
    registry.register(RegisteredTool {
        name: "provisioning.set_fault_injection".to_string(),
        description: DESC_SET_FAULT.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let task_type = req_str(&args, "task_type")?;
                let fault_type = req_str(&args, "fault_type")?;
                let enabled = args
                    .get("enabled")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                let probability = args.get("probability").and_then(Value::as_f64);
                let injectors = c.list_fault_injection().await.map_err(map_err)?;
                let target = injectors.as_array().and_then(|arr| {
                    arr.iter().find(|i| {
                        i.get("taskType") == Some(&json!(task_type))
                            && i.get("faultType") == Some(&json!(fault_type))
                    })
                });
                match target.and_then(|t| t.get("id")).and_then(Value::as_str) {
                    None => Ok(json!({
                        "error": "NOT_FOUND",
                        "message": format!(
                            "No fault-injection configured for {task_type}/{fault_type}."
                        ),
                    })),
                    Some(id) => c
                        .update_fault_injection(id, Some(enabled), probability, None)
                        .await
                        .map_err(map_err),
                }
            }
            .boxed()
        }),
    });
}
