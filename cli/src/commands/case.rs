//! `bss case ...` — case lifecycle commands. Port of
//! `cli/bss_cli/commands/case.py`.

use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::ClientError;
use bss_cockpit::renderers::tables::render_case;
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, Clients};

#[derive(Args)]
pub struct CaseArgs {
    #[command(subcommand)]
    command: CaseCommand,
}

#[derive(Subcommand)]
enum CaseCommand {
    /// Open a new case against a customer.
    Open {
        #[arg(long)]
        customer: String,
        #[arg(long)]
        subject: String,
        #[arg(long, default_value = "technical")]
        category: String,
        #[arg(long, default_value = "medium")]
        priority: String,
    },
    /// List cases, optionally filtered.
    List {
        #[arg(long)]
        customer: Option<String>,
        #[arg(long)]
        state: Option<String>,
    },
    /// Render a case with its tickets and notes.
    Show { case_id: String },
    /// Close a case (policy: no open tickets) (destructive).
    Close {
        case_id: String,
        #[arg(long, default_value = "resolved")]
        resolution: String,
        #[arg(long = "allow-destructive")]
        allow_destructive: bool,
    },
}

pub async fn run(args: CaseArgs) -> ExitCode {
    match args.command {
        CaseCommand::Open {
            customer,
            subject,
            category,
            priority,
        } => run_safely(move |c| open(c, customer, subject, category, priority)).await,
        CaseCommand::List { customer, state } => {
            run_safely(move |c| list(c, customer, state)).await
        }
        CaseCommand::Show { case_id } => run_safely(move |c| show(c, case_id)).await,
        CaseCommand::Close {
            case_id,
            resolution,
            allow_destructive,
        } => {
            if !allow_destructive {
                eprintln!("close is gated behind --allow-destructive.");
                return ExitCode::from(2);
            }
            run_safely(move |c| close(c, case_id, resolution)).await
        }
    }
}

async fn open(
    c: Arc<Clients>,
    customer: String,
    subject: String,
    category: String,
    priority: String,
) -> Result<(), ClientError> {
    let case = c
        .crm
        .open_case(&customer, &subject, &category, &priority, None, None, None)
        .await?;
    let id = case.get("id").and_then(Value::as_str).unwrap_or("");
    let subj = case.get("subject").and_then(Value::as_str).unwrap_or("");
    let state = case.get("state").and_then(Value::as_str).unwrap_or("");
    // Python: `Opened {id}  {subject!r}  [{state}]` — `!r` single-quotes the subject.
    println!("Opened {id}  {}  [{state}]", py_repr(subj));
    Ok(())
}

async fn list(
    c: Arc<Clients>,
    customer: Option<String>,
    state: Option<String>,
) -> Result<(), ClientError> {
    let rows = c
        .crm
        .list_cases(customer.as_deref(), state.as_deref(), None)
        .await?;
    let empty = Vec::new();
    for r in rows.as_array().unwrap_or(&empty) {
        let id = r.get("id").and_then(Value::as_str).unwrap_or("");
        let subject = r.get("subject").and_then(Value::as_str).unwrap_or("");
        // Python `subject[:30]` truncates by code point, then pads to width 30.
        let subject: String = subject.chars().take(30).collect();
        let priority = r.get("priority").and_then(Value::as_str).unwrap_or("");
        let state = r.get("state").and_then(Value::as_str).unwrap_or("");
        println!("{id:<9}  {subject:<30} {priority:<7} {state}");
    }
    Ok(())
}

async fn show(c: Arc<Clients>, case_id: String) -> Result<(), ClientError> {
    let case = c.crm.get_case(&case_id).await?;
    let tickets = c.crm.list_tickets(None, Some(&case_id), None, None).await?;
    let tickets = tickets.as_array().cloned().unwrap_or_default();
    let notes = case
        .get("notes")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    println!("{}", render_case(&case, &tickets, &notes));
    Ok(())
}

async fn close(c: Arc<Clients>, case_id: String, resolution: String) -> Result<(), ClientError> {
    let out = c.crm.close_case(&case_id, &resolution).await?;
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    let state = out.get("state").and_then(Value::as_str).unwrap_or("");
    println!("Closed {id}  [{state}]");
    Ok(())
}

/// Python's `{s!r}` for a plain string: single-quote wrapped. Matches the cockpit's
/// `bss_csr::cases::py_repr` simplification — an embedded apostrophe would make
/// CPython switch to double quotes, but operator subjects don't exercise that and
/// the two ports stay in lockstep.
fn py_repr(s: &str) -> String {
    format!("'{s}'")
}
