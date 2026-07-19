//! `bss catalog ...` — product catalog commands. Port of
//! `cli/bss_cli/commands/catalog.py`.

use std::process::ExitCode;
use std::sync::Arc;

use bss_cockpit::renderers::catalog::render_catalog;
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, Clients};

#[derive(Args)]
pub struct CatalogArgs {
    #[command(subcommand)]
    command: CatalogCommand,
}

#[derive(Subcommand)]
enum CatalogCommand {
    /// Render the three-column plan comparison.
    List,
    /// List VAS offerings.
    Vas,
    /// Show a single offering (JSON).
    Show {
        /// Offering ID (e.g. PLAN_M).
        offering_id: String,
    },
}

pub async fn run(args: CatalogArgs) -> ExitCode {
    match args.command {
        CatalogCommand::List => run_safely(list).await,
        CatalogCommand::Vas => run_safely(vas).await,
        CatalogCommand::Show { offering_id } => run_safely(move |c| show(c, offering_id)).await,
    }
}

async fn list(c: Arc<Clients>) -> Result<(), bss_clients::ClientError> {
    let offerings = c.catalog.list_offerings().await?;
    let arr = offerings.as_array().cloned().unwrap_or_default();
    println!("{}", render_catalog(&arr));
    Ok(())
}

async fn vas(c: Arc<Clients>) -> Result<(), bss_clients::ClientError> {
    let vas = c.catalog.list_vas().await?;
    let arr = vas.as_array().cloned().unwrap_or_default();
    // The CLI `vas` command hand-formats its own rows (NOT the golden `render_vas
    // _list`, which backs the LLM tool surface): id ljust 20, name ljust 28, then
    // "<currency> <price>". Reproduced exactly.
    for v in &arr {
        println!("{}", vas_row(v));
    }
    Ok(())
}

/// One `bss catalog vas` row. `price = priceAmount or price or "?"`;
/// `currency or "SGD"`.
fn vas_row(v: &Value) -> String {
    let id = v.get("id").and_then(Value::as_str).unwrap_or("");
    let name = v.get("name").and_then(Value::as_str).unwrap_or("");
    let ccy = v.get("currency").and_then(Value::as_str).unwrap_or("SGD");
    let price = v
        .get("priceAmount")
        .or_else(|| v.get("price"))
        .map(scalar)
        .unwrap_or_else(|| "?".to_string());
    // `{:<20} {:<28} {} {}` — width counts chars, matching Python's f-string.
    format!("{id:<20} {name:<28} {ccy} {price}")
}

/// A JSON scalar as Python's f-string renders it (int without `.0`, string bare).
fn scalar(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Number(n) => n
            .as_i64()
            .map(|i| i.to_string())
            .unwrap_or_else(|| n.to_string()),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

async fn show(c: Arc<Clients>, offering_id: String) -> Result<(), bss_clients::ClientError> {
    let offering = c.catalog.get_offering(&offering_id).await?;
    println!("{}", super::pretty(&offering));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn vas_row_matches_python_fstring() {
        let v =
            json!({"id": "VAS_1GB", "name": "1 GB top-up", "currency": "SGD", "priceAmount": 5});
        // id ljust 20, name ljust 28, then "SGD 5" — price is an int, no `.0`.
        assert_eq!(
            vas_row(&v),
            "VAS_1GB              1 GB top-up                  SGD 5"
        );
    }

    #[test]
    fn vas_row_defaults() {
        // Missing currency → SGD; missing price → "?"; missing name → blank.
        let v = json!({"id": "VAS_X"});
        assert_eq!(
            vas_row(&v),
            "VAS_X                                             SGD ?"
        );
        // `price` falls back to the `price` key when `priceAmount` is absent.
        let v = json!({"id": "V", "price": "2.50"});
        assert!(vas_row(&v).ends_with("SGD 2.50"));
    }
}
