//! `bss prov ...` — provisioning-sim (tasks + fault injection). Port of
//! `cli/bss_cli/commands/prov.py`.

use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::ClientError;
use bss_cockpit::renderers::tables::render_prov_tasks;
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, run_safely_code, Clients};

#[derive(Args)]
pub struct ProvArgs {
    #[command(subcommand)]
    command: ProvCommand,
}

#[derive(Subcommand)]
enum ProvCommand {
    /// List provisioning tasks.
    Tasks {
        #[arg(long)]
        service: Option<String>,
        #[arg(long)]
        state: Option<String>,
    },
    /// Show a single provisioning task.
    Show { task_id: String },
    /// Manually resolve a stuck provisioning task.
    Resolve {
        task_id: String,
        #[arg(long)]
        note: String,
    },
    /// Retry a failed provisioning task.
    Retry { task_id: String },
    /// Toggle/adjust fault-injection for a task type (destructive).
    Fault {
        /// e.g. HLR_PROVISION
        task_type: String,
        /// fail_first_attempt | fail_always | stuck | slow
        fault_type: String,
        /// Disable the injector instead of enabling it (Python's `--enable/--disable`,
        /// default enable).
        #[arg(long = "disable")]
        disable: bool,
        #[arg(long)]
        probability: Option<f64>,
        #[arg(long = "allow-destructive")]
        allow_destructive: bool,
    },
}

pub async fn run(args: ProvArgs) -> ExitCode {
    match args.command {
        ProvCommand::Tasks { service, state } => {
            run_safely(move |c| tasks(c, service, state)).await
        }
        ProvCommand::Show { task_id } => run_safely(move |c| show(c, task_id)).await,
        ProvCommand::Resolve { task_id, note } => {
            run_safely(move |c| resolve(c, task_id, note)).await
        }
        ProvCommand::Retry { task_id } => run_safely(move |c| retry(c, task_id)).await,
        ProvCommand::Fault {
            task_type,
            fault_type,
            disable,
            probability,
            allow_destructive,
        } => {
            if !allow_destructive {
                eprintln!("fault is gated behind --allow-destructive.");
                return ExitCode::from(2);
            }
            run_safely_code(move |c| fault(c, task_type, fault_type, !disable, probability)).await
        }
    }
}

async fn tasks(
    c: Arc<Clients>,
    service: Option<String>,
    state: Option<String>,
) -> Result<(), ClientError> {
    let ts = c
        .provisioning
        .list_tasks(service.as_deref(), state.as_deref())
        .await?;
    let arr = ts.as_array().cloned().unwrap_or_default();
    println!("{}", render_prov_tasks(&arr));
    Ok(())
}

async fn show(c: Arc<Clients>, task_id: String) -> Result<(), ClientError> {
    let t = c.provisioning.get_task(&task_id).await?;
    println!("{}", super::pretty(&t));
    Ok(())
}

async fn resolve(c: Arc<Clients>, task_id: String, note: String) -> Result<(), ClientError> {
    let out = c.provisioning.resolve_task(&task_id, &note).await?;
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    let state = out.get("state").and_then(Value::as_str).unwrap_or("");
    println!("{id} → {state}");
    Ok(())
}

async fn retry(c: Arc<Clients>, task_id: String) -> Result<(), ClientError> {
    let out = c.provisioning.retry_task(&task_id).await?;
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    let state = out.get("state").and_then(Value::as_str).unwrap_or("");
    // Python `out.get('attempts')` → None when absent, which f-strings render as
    // the literal "None".
    let attempts = out.get("attempts").map(scalar).unwrap_or_else(none);
    println!("{id} → {state}  attempts={attempts}");
    Ok(())
}

async fn fault(
    c: Arc<Clients>,
    task_type: String,
    fault_type: String,
    enable: bool,
    probability: Option<f64>,
) -> Result<ExitCode, ClientError> {
    let injectors = c.provisioning.list_fault_injection().await?;
    let empty = Vec::new();
    let target = injectors.as_array().unwrap_or(&empty).iter().find(|i| {
        i.get("taskType").and_then(Value::as_str) == Some(task_type.as_str())
            && i.get("faultType").and_then(Value::as_str) == Some(fault_type.as_str())
    });
    let Some(target) = target else {
        eprintln!("No fault-injection for {task_type}/{fault_type}");
        return Ok(ExitCode::from(2));
    };
    let id = target.get("id").and_then(Value::as_str).unwrap_or("");
    let out = c
        .provisioning
        .update_fault_injection(id, Some(enable), probability, None)
        .await?;
    let out_id = out.get("id").and_then(Value::as_str).unwrap_or("");
    // Python renders bool via f-string as True/False; `enabled` is always present.
    let enabled = match out.get("enabled").and_then(Value::as_bool) {
        Some(true) => "True",
        _ => "False",
    };
    let p = out.get("probability").map(scalar).unwrap_or_else(none);
    println!("{out_id} enabled={enabled} p={p}");
    Ok(ExitCode::SUCCESS)
}

fn none() -> String {
    "None".to_string()
}

/// A JSON scalar as Python's f-string renders it (bare number, `None` for null,
/// `True`/`False` for bools).
fn scalar(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".to_string(),
        Value::Number(n) => n.to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        other => other.to_string(),
    }
}
