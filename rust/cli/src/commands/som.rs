//! `bss som ...` — SOM inventory inspection. Port of `cli/bss_cli/commands/som.py`.

use std::process::ExitCode;
use std::sync::Arc;

use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, Clients};

#[derive(Args)]
pub struct SomArgs {
    #[command(subcommand)]
    command: SomCommand,
}

#[derive(Subcommand)]
enum SomCommand {
    /// Service inventory (TMF638).
    Service(ServiceArgs),
    /// Show a single service (JSON).
    ServiceShow { service_id: String },
    /// Show a service order (JSON).
    SoShow { service_order_id: String },
}

#[derive(Args)]
struct ServiceArgs {
    #[command(subcommand)]
    command: ServiceCommand,
}

#[derive(Subcommand)]
enum ServiceCommand {
    /// List services belonging to a subscription (CFS + RFS tree, flat).
    List {
        #[arg(long)]
        subscription: String,
    },
}

pub async fn run(args: SomArgs) -> ExitCode {
    match args.command {
        SomCommand::Service(sa) => match sa.command {
            ServiceCommand::List { subscription } => {
                run_safely(move |c| service_list(c, subscription)).await
            }
        },
        SomCommand::ServiceShow { service_id } => {
            run_safely(move |c| service_show(c, service_id)).await
        }
        SomCommand::SoShow { service_order_id } => {
            run_safely(move |c| so_show(c, service_order_id)).await
        }
    }
}

async fn service_list(
    c: Arc<Clients>,
    subscription: String,
) -> Result<(), bss_clients::ClientError> {
    let services = c.som.list_services_for_subscription(&subscription).await?;
    let empty = Vec::new();
    for s in services.as_array().unwrap_or(&empty) {
        let id = s.get("id").and_then(Value::as_str).unwrap_or("");
        let stype = s.get("serviceType").and_then(Value::as_str).unwrap_or("");
        let name = s.get("name").and_then(Value::as_str).unwrap_or("");
        let state = s.get("state").and_then(Value::as_str).unwrap_or("");
        // `{id:<9} {serviceType:<4} {name:<22} {state}` — matches the Python f-string.
        println!("{id:<9} {stype:<4} {name:<22} {state}");
    }
    Ok(())
}

async fn service_show(c: Arc<Clients>, service_id: String) -> Result<(), bss_clients::ClientError> {
    let svc = c.som.get_service(&service_id).await?;
    println!("{}", super::pretty(&svc));
    Ok(())
}

async fn so_show(
    c: Arc<Clients>,
    service_order_id: String,
) -> Result<(), bss_clients::ClientError> {
    let so = c.som.get_service_order(&service_order_id).await?;
    println!("{}", super::pretty(&so));
    Ok(())
}
