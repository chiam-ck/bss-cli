//! `bss inventory ...` — MSISDN + eSIM pool browsing + replenishment. Port of
//! `cli/bss_cli/commands/inventory.py`. Inventory is hosted inside the CRM service
//! under `/inventory-api/v1/`; list/show are read-only, `msisdn add-range` is the
//! v0.17 operator-only write.

use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::ClientError;
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, Clients};

#[derive(Args)]
pub struct InventoryArgs {
    #[command(subcommand)]
    command: InventoryCommand,
}

#[derive(Subcommand)]
enum InventoryCommand {
    /// MSISDN pool.
    Msisdn(MsisdnArgs),
    /// eSIM profile pool.
    Esim(EsimArgs),
}

#[derive(Args)]
struct MsisdnArgs {
    #[command(subcommand)]
    command: MsisdnCommand,
}

#[derive(Subcommand)]
enum MsisdnCommand {
    /// List MSISDNs in the pool.
    List {
        /// available | reserved | assigned | released
        #[arg(long)]
        state: Option<String>,
        /// MSISDN prefix filter.
        #[arg(long)]
        prefix: Option<String>,
        #[arg(long, default_value_t = 20)]
        limit: i64,
    },
    /// Show one MSISDN (JSON).
    Show { msisdn: String },
    /// v0.17 — bulk-extend the MSISDN pool (operator-only).
    AddRange {
        /// Numeric prefix, 4–7 digits, e.g. 9100
        prefix: String,
        /// Numbers to add (1..10000)
        count: i64,
    },
}

#[derive(Args)]
struct EsimArgs {
    #[command(subcommand)]
    command: EsimCommand,
}

#[derive(Subcommand)]
enum EsimCommand {
    /// List eSIM profiles in the pool.
    List {
        /// available | reserved | activated | recycled
        #[arg(long)]
        state: Option<String>,
        #[arg(long, default_value_t = 20)]
        limit: i64,
    },
    /// Show one eSIM profile (JSON).
    Show { iccid: String },
    /// Show the LPA activation code + IMSI for an eSIM.
    Activation { iccid: String },
}

pub async fn run(args: InventoryArgs) -> ExitCode {
    match args.command {
        InventoryCommand::Msisdn(m) => match m.command {
            MsisdnCommand::List {
                state,
                prefix,
                limit,
            } => run_safely(move |c| msisdn_list(c, state, prefix, limit)).await,
            MsisdnCommand::Show { msisdn } => run_safely(move |c| msisdn_show(c, msisdn)).await,
            MsisdnCommand::AddRange { prefix, count } => {
                run_safely(move |c| msisdn_add_range(c, prefix, count)).await
            }
        },
        InventoryCommand::Esim(e) => match e.command {
            EsimCommand::List { state, limit } => {
                run_safely(move |c| esim_list(c, state, limit)).await
            }
            EsimCommand::Show { iccid } => run_safely(move |c| esim_show(c, iccid)).await,
            EsimCommand::Activation { iccid } => {
                run_safely(move |c| esim_activation(c, iccid)).await
            }
        },
    }
}

async fn msisdn_list(
    c: Arc<Clients>,
    state: Option<String>,
    prefix: Option<String>,
    limit: i64,
) -> Result<(), ClientError> {
    let rows = c
        .inventory
        .list_msisdns(state.as_deref(), prefix.as_deref(), limit)
        .await?;
    let empty = Vec::new();
    let rows = rows.as_array().unwrap_or(&empty);
    if rows.is_empty() {
        println!("no MSISDNs match");
        return Ok(());
    }
    // Python renders a `rich.Table`; the box-drawing chrome is a documented CLI seam
    // (same as `bss promo show`). The cell values match Python's extraction exactly.
    println!("MSISDN pool ({} shown)", rows.len());
    println!("msisdn        state       assigned to");
    for r in rows {
        let msisdn = str_or(r, &["msisdn"], "?");
        let state = str_or(r, &["status", "state"], "?");
        let assigned = str_or_dash(r, "assigned_to_subscription_id");
        println!("{msisdn}  {state}  {assigned}");
    }
    Ok(())
}

async fn msisdn_show(c: Arc<Clients>, msisdn: String) -> Result<(), ClientError> {
    let m = c.inventory.get_msisdn(&msisdn).await?;
    println!("{}", super::pretty(&m));
    Ok(())
}

async fn msisdn_add_range(c: Arc<Clients>, prefix: String, count: i64) -> Result<(), ClientError> {
    let out = c.inventory.add_msisdn_range(&prefix, count).await?;
    // Python `.get()` renders the literal "None" when absent; `…` is U+2026.
    let inserted = get_or_none(&out, "inserted");
    let skipped = get_or_none(&out, "skipped");
    let first = get_or_none(&out, "first");
    let last = get_or_none(&out, "last");
    println!("inserted {inserted} / skipped {skipped}  ({first} … {last})");
    Ok(())
}

async fn esim_list(c: Arc<Clients>, state: Option<String>, limit: i64) -> Result<(), ClientError> {
    let rows = c.inventory.list_esims(state.as_deref(), limit).await?;
    let empty = Vec::new();
    let rows = rows.as_array().unwrap_or(&empty);
    if rows.is_empty() {
        println!("no eSIM profiles match");
        return Ok(());
    }
    println!("eSIM pool ({} shown)", rows.len());
    println!("iccid        state       msisdn");
    for r in rows {
        let iccid = str_or(r, &["iccid"], "?");
        let state = str_or(r, &["profile_state", "status", "state"], "?");
        let msisdn =
            first_str(r, &["assigned_msisdn", "msisdn"]).unwrap_or_else(|| "—".to_string());
        println!("{iccid}  {state}  {msisdn}");
    }
    Ok(())
}

async fn esim_show(c: Arc<Clients>, iccid: String) -> Result<(), ClientError> {
    let e = c.inventory.get_esim(&iccid).await?;
    println!("{}", super::pretty(&e));
    Ok(())
}

async fn esim_activation(c: Arc<Clients>, iccid: String) -> Result<(), ClientError> {
    let a = c.inventory.get_activation_code(&iccid).await?;
    println!("{}", super::pretty(&a));
    Ok(())
}

/// First key present as a string, else `fallback` — Python's chained
/// `r.get("a", r.get("b", "?"))` where the real payload always carries a string.
fn str_or(r: &Value, keys: &[&str], fallback: &str) -> String {
    first_str(r, keys).unwrap_or_else(|| fallback.to_string())
}

/// First of `keys` present as a non-empty string.
fn first_str(r: &Value, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|k| r.get(*k).and_then(Value::as_str))
        .map(str::to_string)
}

/// Python `r.get(key) or "—"` — falsy (absent/null/empty) ⇒ em-dash.
fn str_or_dash(r: &Value, key: &str) -> String {
    match r.get(key).and_then(Value::as_str) {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => "—".to_string(),
    }
}

/// `out.get(key)` as an f-string renders it — the literal "None" when absent.
fn get_or_none(v: &Value, key: &str) -> String {
    match v.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => "None".to_string(),
        Some(other) => other.to_string(),
    }
}
