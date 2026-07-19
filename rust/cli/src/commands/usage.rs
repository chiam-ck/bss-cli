//! `bss usage ...` — mediation / usage simulation. Port of
//! `cli/bss_cli/commands/usage.py`.

use std::process::ExitCode;
use std::sync::Arc;

use chrono::SubsecRound;
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, Clients};

#[derive(Args)]
pub struct UsageArgs {
    #[command(subcommand)]
    command: UsageCommand,
}

#[derive(Subcommand)]
enum UsageCommand {
    /// Submit a single usage event to mediation.
    Simulate {
        #[arg(long)]
        msisdn: String,
        /// data | voice_minutes | sms
        #[arg(long = "type", default_value = "data")]
        type_: String,
        /// e.g. 1GB, 500MB, 5, 3min
        #[arg(long, default_value = "1")]
        quantity: String,
    },
}

pub async fn run(args: UsageArgs) -> ExitCode {
    match args.command {
        UsageCommand::Simulate {
            msisdn,
            type_,
            quantity,
        } => run_safely(move |c| simulate(c, msisdn, type_, quantity)).await,
    }
}

async fn simulate(
    c: Arc<Clients>,
    msisdn: String,
    event_type: String,
    quantity: String,
) -> Result<(), bss_clients::ClientError> {
    let (qty, unit) = parse_quantity(&quantity, &event_type);
    // `clock_now().replace(microsecond=0).isoformat()`.
    let now = bss_clock::now().trunc_subsecs(0);
    let event_time = now.format("%Y-%m-%dT%H:%M:%S%:z").to_string();
    let out = c
        .mediation
        .submit_usage_full(
            &msisdn,
            &event_type,
            &event_time,
            qty,
            &unit,
            Some("cli"), // the CLI stamps source=cli on the event
            None,
            false,
        )
        .await?;
    let state = if out
        .get("processed")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        "processed"
    } else {
        "rejected"
    };
    let id = out.get("id").and_then(Value::as_str).unwrap_or("");
    println!("{id}  {state}  msisdn={msisdn} {qty}{unit}");
    Ok(())
}

/// Parse e.g. `1GB` → `(1024, "mb")`, `500MB` → `(500, "mb")`, `3min` →
/// `(3, "minutes")`. Falls back to a plain integer with a type-defaulted unit.
/// Port of `_parse_quantity` (regex `^(\d+(?:\.\d+)?)\s*([A-Za-z]+)$`).
fn parse_quantity(raw: &str, event_type: &str) -> (i64, String) {
    let s = raw.trim();
    match split_qty(s) {
        Some((value, unit)) => {
            let unit = unit.to_lowercase();
            match unit.as_str() {
                "gb" => ((value * 1024.0) as i64, "mb".to_string()),
                "mb" => (value as i64, "mb".to_string()),
                "min" | "mins" | "minutes" => (value as i64, "minutes".to_string()),
                "sms" | "count" | "ct" => (value as i64, "count".to_string()),
                other => (value as i64, other.to_string()),
            }
        }
        // No unit suffix → `int(float(raw))` with the event-type default unit.
        None => {
            let value = s.parse::<f64>().unwrap_or(0.0) as i64;
            (value, default_unit(event_type))
        }
    }
}

/// Split a `<number><letters>` string into `(number, letters)` — the captures of
/// the Python regex. Returns `None` when the whole string isn't that shape.
fn split_qty(s: &str) -> Option<(f64, &str)> {
    let split = s.find(|c: char| c.is_ascii_alphabetic())?;
    let (num, unit) = s.split_at(split);
    let num = num.trim();
    // The unit must be all letters; the number must parse and be non-empty.
    if unit.is_empty() || !unit.chars().all(|c| c.is_ascii_alphabetic()) {
        return None;
    }
    num.parse::<f64>().ok().map(|v| (v, unit))
}

fn default_unit(event_type: &str) -> String {
    match event_type {
        "data" => "mb",
        "voice_minutes" => "minutes",
        "sms" => "count",
        _ => "count",
    }
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_quantity_units() {
        assert_eq!(parse_quantity("1GB", "data"), (1024, "mb".to_string()));
        assert_eq!(parse_quantity("500MB", "data"), (500, "mb".to_string()));
        assert_eq!(
            parse_quantity("3min", "voice_minutes"),
            (3, "minutes".to_string())
        );
        assert_eq!(parse_quantity("5 sms", "sms"), (5, "count".to_string()));
        // Unknown unit passes through lowercased.
        assert_eq!(parse_quantity("2foo", "data"), (2, "foo".to_string()));
    }

    #[test]
    fn parse_quantity_bare_number_uses_type_default() {
        assert_eq!(parse_quantity("5", "data"), (5, "mb".to_string()));
        assert_eq!(
            parse_quantity("10", "voice_minutes"),
            (10, "minutes".to_string())
        );
        assert_eq!(parse_quantity("7", "sms"), (7, "count".to_string()));
        // int(float("2.9")) truncates to 2.
        assert_eq!(parse_quantity("2.9", "sms"), (2, "count".to_string()));
        // An unknown type defaults to count.
        assert_eq!(parse_quantity("3", "mystery"), (3, "count".to_string()));
    }
}
