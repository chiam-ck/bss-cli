//! `bss ticket ...` — trouble-ticket lifecycle commands. Port of
//! `cli/bss_cli/commands/ticket.py`.

use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::{ticket_in_progress_trigger, ticket_trigger_for_state, ClientError};
use bss_cockpit::renderers::tables::render_ticket;
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, Clients};

#[derive(Args)]
pub struct TicketArgs {
    #[command(subcommand)]
    command: TicketCommand,
}

#[derive(Subcommand)]
enum TicketCommand {
    /// Open a trouble ticket, optionally linked to a case/customer.
    Open {
        #[arg(long = "type")]
        ticket_type: String,
        #[arg(long)]
        subject: String,
        #[arg(long)]
        case: Option<String>,
        #[arg(long)]
        customer: Option<String>,
    },
    /// List trouble tickets, optionally filtered.
    List {
        #[arg(long)]
        case: Option<String>,
        #[arg(long)]
        customer: Option<String>,
        #[arg(long)]
        state: Option<String>,
        #[arg(long)]
        agent: Option<String>,
    },
    /// Show a trouble ticket.
    Show { ticket_id: String },
    /// Assign a ticket to an agent.
    Assign {
        ticket_id: String,
        #[arg(long)]
        agent: String,
    },
    /// Transition ticket → acknowledged.
    Ack { ticket_id: String },
    /// Transition ticket → in_progress.
    Start { ticket_id: String },
    /// Resolve a ticket with resolution notes.
    Resolve {
        ticket_id: String,
        #[arg(long)]
        notes: String,
    },
    /// Transition ticket → closed.
    Close { ticket_id: String },
    /// Cancel a ticket (destructive).
    Cancel {
        ticket_id: String,
        #[arg(long = "allow-destructive")]
        allow_destructive: bool,
    },
}

pub async fn run(args: TicketArgs) -> ExitCode {
    match args.command {
        TicketCommand::Open {
            ticket_type,
            subject,
            case,
            customer,
        } => run_safely(move |c| open(c, ticket_type, subject, case, customer)).await,
        TicketCommand::List {
            case,
            customer,
            state,
            agent,
        } => run_safely(move |c| list(c, case, customer, state, agent)).await,
        TicketCommand::Show { ticket_id } => run_safely(move |c| show(c, ticket_id)).await,
        TicketCommand::Assign { ticket_id, agent } => {
            run_safely(move |c| assign(c, ticket_id, agent)).await
        }
        TicketCommand::Ack { ticket_id } => {
            run_safely(move |c| transition(c, ticket_id, "acknowledged")).await
        }
        TicketCommand::Start { ticket_id } => {
            run_safely(move |c| transition(c, ticket_id, "in_progress")).await
        }
        TicketCommand::Resolve { ticket_id, notes } => {
            run_safely(move |c| resolve(c, ticket_id, notes)).await
        }
        TicketCommand::Close { ticket_id } => {
            run_safely(move |c| transition(c, ticket_id, "closed")).await
        }
        TicketCommand::Cancel {
            ticket_id,
            allow_destructive,
        } => {
            if !allow_destructive {
                eprintln!("cancel is gated behind --allow-destructive.");
                return ExitCode::from(2);
            }
            run_safely(move |c| cancel(c, ticket_id)).await
        }
    }
}

async fn open(
    c: Arc<Clients>,
    ticket_type: String,
    subject: String,
    case: Option<String>,
    customer: Option<String>,
) -> Result<(), ClientError> {
    let t = c
        .crm
        .open_ticket(
            &ticket_type,
            &subject,
            case.as_deref(),
            customer.as_deref(),
            None,
            None,
            None,
        )
        .await?;
    let id = t.get("id").and_then(Value::as_str).unwrap_or("");
    let ty = t.get("ticketType").and_then(Value::as_str).unwrap_or("");
    let state = t.get("state").and_then(Value::as_str).unwrap_or("");
    println!("Opened {id}  {ty}  [{state}]");
    Ok(())
}

async fn list(
    c: Arc<Clients>,
    case: Option<String>,
    customer: Option<String>,
    state: Option<String>,
    agent: Option<String>,
) -> Result<(), ClientError> {
    let rows = c
        .crm
        .list_tickets(
            customer.as_deref(),
            case.as_deref(),
            state.as_deref(),
            agent.as_deref(),
        )
        .await?;
    let empty = Vec::new();
    for r in rows.as_array().unwrap_or(&empty) {
        let id = r.get("id").and_then(Value::as_str).unwrap_or("");
        let ty = r.get("ticketType").and_then(Value::as_str).unwrap_or("");
        let state = r.get("state").and_then(Value::as_str).unwrap_or("");
        let priority = r.get("priority").and_then(Value::as_str).unwrap_or("");
        // Python: `r.get('assignedAgent') or '—'` — em-dash when null/empty.
        let agent = match r.get("assignedAgent").and_then(Value::as_str) {
            Some(a) if !a.is_empty() => a,
            _ => "—",
        };
        println!("{id:<8}  {ty:<18} {state:<12} {priority:<6} {agent}");
    }
    Ok(())
}

async fn show(c: Arc<Clients>, ticket_id: String) -> Result<(), ClientError> {
    let t = c.crm.get_ticket(&ticket_id).await?;
    println!("{}", render_ticket(&t));
    Ok(())
}

async fn assign(c: Arc<Clients>, ticket_id: String, agent: String) -> Result<(), ClientError> {
    let out = c.crm.assign_ticket(&ticket_id, &agent).await?;
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    // Python: `→ {out.get('assignedAgent')}` — renders the literal "None" when absent.
    let assigned = out
        .get("assignedAgent")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| "None".to_string());
    println!("Assigned {id} → {assigned}");
    Ok(())
}

/// Map a ticket target state → trigger (reading current state for `in_progress`),
/// then POST the transition. Mirrors the client's `transition_ticket(to_state=…)`,
/// which the Rust client doesn't carry (it takes a resolved trigger). Backs
/// `ack` (→acknowledged), `start` (→in_progress), `close` (→closed).
async fn transition(c: Arc<Clients>, ticket_id: String, to_state: &str) -> Result<(), ClientError> {
    let trigger = if to_state == "in_progress" {
        let current = c.crm.get_ticket(&ticket_id).await?;
        let src = current.get("state").and_then(Value::as_str).unwrap_or("");
        ticket_in_progress_trigger(src)
            .map(str::to_string)
            .unwrap_or_else(|| to_state.to_string())
    } else {
        ticket_trigger_for_state(to_state)
            .map(str::to_string)
            .unwrap_or_else(|| to_state.to_string())
    };
    let out = c.crm.transition_ticket(&ticket_id, &trigger).await?;
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    let state = out.get("state").and_then(Value::as_str).unwrap_or("");
    println!("{id} → {state}");
    Ok(())
}

async fn resolve(c: Arc<Clients>, ticket_id: String, notes: String) -> Result<(), ClientError> {
    let out = c.crm.resolve_ticket(&ticket_id, &notes).await?;
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    let state = out.get("state").and_then(Value::as_str).unwrap_or("");
    println!("{id} → {state}");
    Ok(())
}

async fn cancel(c: Arc<Clients>, ticket_id: String) -> Result<(), ClientError> {
    let out = c.crm.cancel_ticket(&ticket_id).await?;
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    let state = out.get("state").and_then(Value::as_str).unwrap_or("");
    println!("{id} → {state}");
    Ok(())
}
