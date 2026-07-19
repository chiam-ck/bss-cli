//! `bss subscription ...` — subscription + VAS commands. Port of
//! `cli/bss_cli/commands/subscription.py`.

use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::ClientError;
use bss_cockpit::renderers::esim::render_esim_activation;
use bss_cockpit::renderers::subscription::{render_subscription, SubscriptionCtx};
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, Clients};

#[derive(Args)]
pub struct SubscriptionArgs {
    #[command(subcommand)]
    command: SubscriptionCommand,
}

#[derive(Subcommand)]
enum SubscriptionCommand {
    /// Render a subscription (bundle bars + state). `--show-esim` appends the
    /// eSIM activation card.
    Show {
        subscription_id: String,
        #[arg(long = "show-esim")]
        show_esim: bool,
    },
    /// List subscriptions for a customer.
    List {
        #[arg(long)]
        customer: String,
    },
    /// Purchase a VAS top-up for a subscription (charged to COF).
    Vas {
        subscription_id: String,
        /// e.g. VAS_DATA_5GB
        vas_offering_id: String,
    },
    /// Manually renew a subscription (normally automatic at period boundary).
    Renew { subscription_id: String },
    /// Terminate a subscription (destructive).
    Terminate {
        subscription_id: String,
        #[arg(long = "allow-destructive")]
        allow_destructive: bool,
    },
}

pub async fn run(args: SubscriptionArgs) -> ExitCode {
    match args.command {
        SubscriptionCommand::Show {
            subscription_id,
            show_esim,
        } => run_safely(move |c| show(c, subscription_id, show_esim)).await,
        SubscriptionCommand::List { customer } => run_safely(move |c| list(c, customer)).await,
        SubscriptionCommand::Vas {
            subscription_id,
            vas_offering_id,
        } => run_safely(move |c| vas(c, subscription_id, vas_offering_id)).await,
        SubscriptionCommand::Renew { subscription_id } => {
            run_safely(move |c| renew(c, subscription_id)).await
        }
        SubscriptionCommand::Terminate {
            subscription_id,
            allow_destructive,
        } => {
            // The destructive gate is a pure CLI check, before any client work —
            // matches the Python `if not allow_destructive: … Exit(2)`.
            if !allow_destructive {
                eprintln!("terminate is gated behind --allow-destructive.");
                return ExitCode::from(2);
            }
            run_safely(move |c| terminate(c, subscription_id)).await
        }
    }
}

async fn show(
    c: Arc<Clients>,
    subscription_id: String,
    show_esim: bool,
) -> Result<(), ClientError> {
    let sub = c.subscription.get(&subscription_id).await?;
    // Best-effort enrichment — a missing offering/customer just renders blank.
    let offering = match sub.get("offeringId").and_then(Value::as_str) {
        Some(oid) => c.catalog.get_offering(oid).await.ok(),
        None => None,
    };
    let customer = match sub.get("customerId").and_then(Value::as_str) {
        Some(cid) => c.crm.get_customer(cid).await.ok(),
        None => None,
    };
    let ctx = SubscriptionCtx {
        customer: customer.as_ref(),
        offering: offering.as_ref(),
        esim: None,
        now: None,
    };
    println!("{}", render_subscription(&sub, &ctx));
    if show_esim {
        let act = c.subscription.get_esim_activation(&subscription_id).await?;
        println!("{}", render_esim_activation(&act, false));
    }
    Ok(())
}

async fn list(c: Arc<Clients>, customer: String) -> Result<(), ClientError> {
    let subs = c.subscription.list_for_customer(&customer).await?;
    let empty = Vec::new();
    for s in subs.as_array().unwrap_or(&empty) {
        let id = s.get("id").and_then(Value::as_str).unwrap_or("");
        let offering = s.get("offeringId").and_then(Value::as_str).unwrap_or("");
        let state = s.get("state").and_then(Value::as_str).unwrap_or("");
        let msisdn = s.get("msisdn").and_then(Value::as_str).unwrap_or("");
        // `{id:<8}  {offeringId:<8} {state:<8} MSISDN {msisdn}` — note the double
        // space after the id, matching the Python f-string.
        println!("{id:<8}  {offering:<8} {state:<8} MSISDN {msisdn}");
    }
    Ok(())
}

async fn vas(
    c: Arc<Clients>,
    subscription_id: String,
    vas_offering_id: String,
) -> Result<(), ClientError> {
    let out = c
        .subscription
        .purchase_vas(&subscription_id, &vas_offering_id)
        .await?;
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    let state = out.get("state").and_then(Value::as_str).unwrap_or("");
    println!("{id} → {state}  (+{vas_offering_id})");
    Ok(())
}

async fn renew(c: Arc<Clients>, subscription_id: String) -> Result<(), ClientError> {
    let out = c.subscription.renew(&subscription_id).await?;
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    let next = out
        .get("nextRenewalAt")
        .and_then(Value::as_str)
        .unwrap_or("");
    println!("{id} renewed → next {next}");
    Ok(())
}

async fn terminate(c: Arc<Clients>, subscription_id: String) -> Result<(), ClientError> {
    // Python's bare `terminate(id)` → reason=None, release_inventory=True → no body.
    let out = c
        .subscription
        .terminate_with_reason(&subscription_id, None, true)
        .await?;
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    let state = out.get("state").and_then(Value::as_str).unwrap_or("");
    println!("{id} → {state}");
    Ok(())
}
