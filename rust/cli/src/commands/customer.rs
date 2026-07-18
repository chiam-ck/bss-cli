//! `bss customer ...` — direct customer commands. Port of
//! `cli/bss_cli/commands/customer.py`.

use std::collections::HashMap;
use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::ClientError;
use bss_cockpit::renderers::customer::{render_customer_360, Customer360Ctx};
use bss_orchestrator::tools::payment::local_tokenize_card;
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, run_safely_code, Clients};

#[derive(Args)]
pub struct CustomerArgs {
    #[command(subcommand)]
    command: CustomerCommand,
}

#[derive(Subcommand)]
enum CustomerCommand {
    /// Create a customer; optionally tokenise + attach a card-on-file.
    Create {
        /// Customer display name.
        #[arg(long)]
        name: String,
        #[arg(long)]
        email: Option<String>,
        #[arg(long)]
        phone: Option<String>,
        /// 16-digit PAN; CLI tokenises it client-side (sandbox).
        #[arg(long)]
        card: Option<String>,
    },
    /// List customers, optionally filtered by state or name substring.
    List {
        #[arg(long)]
        state: Option<String>,
        /// Filter by name substring.
        #[arg(long)]
        name: Option<String>,
    },
    /// Render the customer 360 view.
    Show {
        /// Customer ID (CUST-NNN).
        customer_id: String,
    },
}

pub async fn run(args: CustomerArgs) -> ExitCode {
    match args.command {
        CustomerCommand::Create {
            name,
            email,
            phone,
            card,
        } => run_safely_code(move |c| create(c, name, email, phone, card)).await,
        CustomerCommand::List { state, name } => run_safely(move |c| list(c, state, name)).await,
        CustomerCommand::Show { customer_id } => run_safely(move |c| show(c, customer_id)).await,
    }
}

async fn create(
    c: Arc<Clients>,
    name: String,
    email: Option<String>,
    phone: Option<String>,
    card: Option<String>,
) -> Result<ExitCode, ClientError> {
    let customer = c
        .crm
        .create_customer(&name, email.as_deref(), phone.as_deref())
        .await?;
    let id = customer.get("id").and_then(Value::as_str).unwrap_or("");
    let display = individual_display(&customer);
    println!("Created {id}  {display}");
    if let Some(card) = card {
        // Python's `local_tokenize_card` ValueError propagates uncaught → exit 1,
        // after the customer has already been created + printed (partial success).
        let (token, last4, brand) = match local_tokenize_card(&card) {
            Ok(t) => t,
            Err(detail) => {
                eprintln!("{detail}");
                return Ok(ExitCode::from(1));
            }
        };
        let pm = c
            .payment
            .create_payment_method(id, &token, &last4, &brand, 12, 2030)
            .await?;
        let pm_id = pm.get("id").and_then(Value::as_str).unwrap_or("");
        let cs = pm.get("cardSummary");
        let br = cs
            .and_then(|c| c.get("brand"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let l4 = cs
            .and_then(|c| c.get("last4"))
            .and_then(Value::as_str)
            .unwrap_or("");
        println!("Attached card {pm_id}  {br}•••{l4}");
    }
    Ok(ExitCode::SUCCESS)
}

async fn list(
    c: Arc<Clients>,
    state: Option<String>,
    name: Option<String>,
) -> Result<(), ClientError> {
    let rows = c
        .crm
        .list_customers(state.as_deref(), name.as_deref())
        .await?;
    let empty = Vec::new();
    for r in rows.as_array().unwrap_or(&empty) {
        let id = r.get("id").and_then(Value::as_str).unwrap_or("");
        let display = list_display_name(r);
        // `{id:<15}  {display:<30} {status||state}`.
        let status = first_str(r, &["status", "state"]).unwrap_or_default();
        println!("{id:<15}  {display:<30} {status}");
    }
    Ok(())
}

async fn show(c: Arc<Clients>, customer_id: String) -> Result<(), ClientError> {
    let cust = c.crm.get_customer(&customer_id).await?;
    let subs = c.subscription.list_for_customer(&customer_id).await?;
    let cases = c.crm.list_cases(Some(&customer_id), None, None).await?;
    let subs_vec = subs.as_array().cloned().unwrap_or_default();
    let cases_vec = cases.as_array().cloned().unwrap_or_default();
    let mut tickets_by_case: HashMap<String, Vec<Value>> = HashMap::new();
    for case in &cases_vec {
        if let Some(cid) = case.get("id").and_then(Value::as_str) {
            let tickets = c.crm.list_tickets(None, Some(cid), None, None).await?;
            tickets_by_case.insert(
                cid.to_string(),
                tickets.as_array().cloned().unwrap_or_default(),
            );
        }
    }
    let interactions = c.crm.list_interactions(&customer_id, 10).await?;
    let interactions_vec = interactions.as_array().cloned().unwrap_or_default();
    let ctx = Customer360Ctx {
        subscriptions: &subs_vec,
        cases: &cases_vec,
        tickets_by_case,
        interactions: &interactions_vec,
        // Python already limits via the client (limit=10) and passes them straight in.
        interactions_limit: None,
    };
    println!("{}", render_customer_360(&cust, &ctx));
    Ok(())
}

/// `" ".join(givenName, familyName)` (non-empty parts) or `—` — the `create` echo.
fn individual_display(customer: &Value) -> String {
    let ind = customer.get("individual");
    let parts: Vec<&str> = ["givenName", "familyName"]
        .iter()
        .filter_map(|k| ind.and_then(|i| i.get(*k)).and_then(Value::as_str))
        .filter(|s| !s.is_empty())
        .collect();
    if parts.is_empty() {
        "—".to_string()
    } else {
        parts.join(" ")
    }
}

/// `list` display name: given+family, else `individual.name`, else top-level `name`,
/// else `—`.
fn list_display_name(r: &Value) -> String {
    let ind = r.get("individual");
    let parts: Vec<&str> = ["givenName", "familyName"]
        .iter()
        .filter_map(|k| ind.and_then(|i| i.get(*k)).and_then(Value::as_str))
        .filter(|s| !s.is_empty())
        .collect();
    let full = parts.join(" ");
    if !full.is_empty() {
        return full;
    }
    ind.and_then(|i| i.get("name"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or_else(|| {
            r.get("name")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
        })
        .unwrap_or("—")
        .to_string()
}

/// First of `keys` present as a non-empty string.
fn first_str(r: &Value, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|k| r.get(*k).and_then(Value::as_str))
        .map(str::to_string)
}
