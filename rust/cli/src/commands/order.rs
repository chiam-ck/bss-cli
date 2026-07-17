//! `bss order ...` — commercial order commands. Port of
//! `cli/bss_cli/commands/order.py`.

use std::collections::HashMap;
use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::ClientError;
use bss_cockpit::renderers::order::{render_order, OrderCtx};
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, Clients};

#[derive(Args)]
pub struct OrderArgs {
    #[command(subcommand)]
    command: OrderCommand,
}

#[derive(Subcommand)]
enum OrderCommand {
    /// Create and submit a commercial order. Optionally wait for completion.
    Create {
        #[arg(long)]
        customer: String,
        /// Offering ID — see `bss catalog list`.
        #[arg(long)]
        offering: String,
        /// Preferred MSISDN.
        #[arg(long)]
        msisdn: Option<String>,
        /// Return after submit instead of polling to completion. Python's
        /// `--wait/--no-wait` defaults to wait; `--wait` is redundant (the default),
        /// so only the negation is surfaced.
        #[arg(long = "no-wait")]
        no_wait: bool,
    },
    /// Render an order with its SOM decomposition tree.
    Show { order_id: String },
    /// List orders for a customer.
    List {
        #[arg(long)]
        customer: String,
    },
    /// Cancel an order (destructive).
    Cancel {
        order_id: String,
        #[arg(long = "allow-destructive")]
        allow_destructive: bool,
    },
}

pub async fn run(args: OrderArgs) -> ExitCode {
    match args.command {
        OrderCommand::Create {
            customer,
            offering,
            msisdn,
            no_wait,
        } => run_safely(move |c| create(c, customer, offering, msisdn, !no_wait)).await,
        OrderCommand::Show { order_id } => run_safely(move |c| show(c, order_id)).await,
        OrderCommand::List { customer } => run_safely(move |c| list(c, customer)).await,
        OrderCommand::Cancel {
            order_id,
            allow_destructive,
        } => {
            if !allow_destructive {
                eprintln!("cancel is gated behind --allow-destructive.");
                return ExitCode::from(2);
            }
            run_safely(move |c| cancel(c, order_id)).await
        }
    }
}

async fn create(
    c: Arc<Clients>,
    customer: String,
    offering: String,
    msisdn: Option<String>,
    wait: bool,
) -> Result<(), ClientError> {
    let o = c
        .com
        .create_order(&customer, &offering, msisdn.as_deref(), None, None, false)
        .await?;
    let id = o
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    println!("Created {id}  {offering}  [{}]", state_of(&o));
    let o = c.com.submit_order(&id).await?;
    println!("Submitted {id}  [{}]", state_of(&o));
    if wait {
        // Python: wait_until(id, target_state="completed", timeout_s=30); the
        // client default poll interval is 0.5s.
        let o = c.com.wait_until(&id, "completed", 30.0, 0.5).await?;
        println!("Final {id}  [{}]", state_of(&o));
    }
    Ok(())
}

async fn show(c: Arc<Clients>, order_id: String) -> Result<(), ClientError> {
    let o = c.com.get_order(&order_id).await?;
    let service_orders = c
        .som
        .list_for_order(&order_id)
        .await?
        .as_array()
        .cloned()
        .unwrap_or_default();

    // The subscription id is on an order item once activation lands.
    let empty = Vec::new();
    let mut sub_id: Option<String> = None;
    for item in o.get("items").and_then(Value::as_array).unwrap_or(&empty) {
        if let Some(s) = item.get("targetSubscriptionId").and_then(Value::as_str) {
            sub_id = Some(s.to_string());
        }
    }

    let mut services_by_so: HashMap<String, Vec<Value>> = HashMap::new();
    let mut tasks_by_service: HashMap<String, Vec<Value>> = HashMap::new();
    if let Some(sid) = &sub_id {
        let services = c
            .som
            .list_services_for_subscription(sid)
            .await?
            .as_array()
            .cloned()
            .unwrap_or_default();
        // Naive: attribute all services to the first service-order (as Python does).
        if let Some(first) = service_orders.first() {
            if let Some(soid) = first.get("id").and_then(Value::as_str) {
                services_by_so.insert(soid.to_string(), services.clone());
            }
        }
        for svc in &services {
            if let Some(svc_id) = svc.get("id").and_then(Value::as_str) {
                let tasks = c
                    .provisioning
                    .list_tasks(Some(svc_id), None)
                    .await?
                    .as_array()
                    .cloned()
                    .unwrap_or_default();
                tasks_by_service.insert(svc_id.to_string(), tasks);
            }
        }
    }

    let ctx = OrderCtx {
        service_orders: &service_orders,
        services_by_so,
        tasks_by_service,
        subscription_id: sub_id,
    };
    println!("{}", render_order(&o, &ctx));
    Ok(())
}

async fn list(c: Arc<Clients>, customer: String) -> Result<(), ClientError> {
    let orders = c.com.list_orders(Some(&customer)).await?;
    let empty = Vec::new();
    for o in orders.as_array().unwrap_or(&empty) {
        let id = o.get("id").and_then(Value::as_str).unwrap_or("");
        let offer = o
            .get("items")
            .and_then(Value::as_array)
            .and_then(|items| items.first())
            .and_then(|i| i.get("offeringId"))
            .and_then(Value::as_str)
            .unwrap_or("—");
        let state = o.get("state").and_then(Value::as_str).unwrap_or("");
        let date = o.get("orderDate").and_then(Value::as_str).unwrap_or("");
        println!("{id:<9}  {offer:<8}  [{state}]  {date}");
    }
    Ok(())
}

async fn cancel(c: Arc<Clients>, order_id: String) -> Result<(), ClientError> {
    let o = c.com.cancel_order(&order_id).await?;
    let id = o.get("id").and_then(Value::as_str).unwrap_or("");
    println!("{id} → {}", state_of(&o));
    Ok(())
}

fn state_of(o: &Value) -> String {
    o.get("state")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}
