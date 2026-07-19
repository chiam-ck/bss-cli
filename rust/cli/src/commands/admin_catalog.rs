//! `bss admin catalog ...` — operator catalog management. Port of
//! `cli/bss_cli/commands/admin_catalog.py`. CLI-only by design (NOT on the LLM tool
//! surface): catalog edits are a deliberate, audited operator task. Each command is a
//! thin wrapper over the catalog/subscription service methods via `bss-clients`.

use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::ClientError;
use clap::{Args, Subcommand};
use serde_json::Value;

use super::{normalize_decimal, parse_iso};
use crate::runtime::{run_safely, Clients};

#[derive(Args)]
pub struct AdminCatalogArgs {
    #[command(subcommand)]
    command: AdminCatalogCommand,
}

#[derive(Subcommand)]
#[allow(clippy::large_enum_variant)]
enum AdminCatalogCommand {
    /// Add a new product offering with its recurring price + bundle allowances.
    AddOffering {
        #[arg(long = "id")]
        offering_id: String,
        #[arg(long)]
        name: String,
        /// Recurring price amount.
        #[arg(long)]
        price: String,
        #[arg(long, default_value = "SGD")]
        currency: String,
        #[arg(long = "valid-from")]
        valid_from: Option<String>,
        #[arg(long = "valid-to")]
        valid_to: Option<String>,
        #[arg(long = "data-mb")]
        data_mb: Option<i64>,
        #[arg(long = "voice-min")]
        voice_min: Option<i64>,
        #[arg(long = "sms-count")]
        sms_count: Option<i64>,
        /// Roaming data allowance in MB (v0.17+ first-class allowance; 0 permitted).
        #[arg(long = "data-roaming-mb")]
        data_roaming_mb: Option<i64>,
    },
    /// Insert a new product_offering_price row, optionally retiring the current.
    SetPrice {
        #[arg(long)]
        offering: String,
        #[arg(long)]
        amount: String,
        #[arg(long = "valid-from")]
        valid_from: String,
        #[arg(long = "valid-to")]
        valid_to: Option<String>,
        #[arg(long, default_value = "SGD")]
        currency: String,
        /// Override generated PRICE_<offering>_<ts>.
        #[arg(long = "price-id")]
        price_id: Option<String>,
        /// Stamp valid_to on the current active row(s) so the new row takes over.
        #[arg(long = "retire-current")]
        retire_current: bool,
    },
    /// Set valid_from/valid_to on an existing offering (retire or launch a promo).
    WindowOffering {
        #[arg(long = "id")]
        offering_id: String,
        #[arg(long = "valid-from")]
        valid_from: Option<String>,
        #[arg(long = "valid-to")]
        valid_to: Option<String>,
    },
    /// Retire an offering — unsellable, lifecycle_status=retired, valid_to=now.
    RetireOffering {
        #[arg(long = "id")]
        offering_id: String,
    },
    /// Migrate every active subscription on --offering to --new-price-id with notice.
    MigratePrice {
        #[arg(long)]
        offering: String,
        #[arg(long = "new-price-id")]
        new_price_id: String,
        #[arg(long = "effective-from")]
        effective_from: String,
        #[arg(long = "notice-days", default_value_t = 30)]
        notice_days: i64,
        /// Operator id stamped into the audit trail.
        #[arg(long = "initiated-by", default_value = "ops")]
        initiated_by: String,
    },
    /// Render the active catalog at a given moment.
    Show {
        /// ISO-8601 moment; defaults to now.
        #[arg(long)]
        at: Option<String>,
    },
}

pub async fn run(args: AdminCatalogArgs) -> ExitCode {
    match args.command {
        AdminCatalogCommand::AddOffering {
            offering_id,
            name,
            price,
            currency,
            valid_from,
            valid_to,
            data_mb,
            voice_min,
            sms_count,
            data_roaming_mb,
        } => {
            // `str(Decimal(price))` + the two `_parse_iso`s run before any async work.
            let amount = match normalize_decimal(&price) {
                Some(v) => v,
                None => return decimal_err(&price),
            };
            let vf = match opt_iso(&valid_from) {
                Ok(v) => v,
                Err(()) => return iso_err(valid_from.as_deref().unwrap_or("")),
            };
            let vt = match opt_iso(&valid_to) {
                Ok(v) => v,
                Err(()) => return iso_err(valid_to.as_deref().unwrap_or("")),
            };
            run_safely(move |c| {
                add_offering(
                    c,
                    offering_id,
                    name,
                    amount,
                    currency,
                    vf,
                    vt,
                    data_mb,
                    voice_min,
                    sms_count,
                    data_roaming_mb,
                )
            })
            .await
        }
        AdminCatalogCommand::SetPrice {
            offering,
            amount,
            valid_from,
            valid_to,
            currency,
            price_id,
            retire_current,
        } => {
            let parsed_amount = match normalize_decimal(&amount) {
                Some(v) => v,
                None => return decimal_err(&amount),
            };
            let vf = match parse_iso(&valid_from) {
                Ok(v) => v,
                Err(()) => return iso_err(&valid_from),
            };
            let vt = match opt_iso(&valid_to) {
                Ok(v) => v,
                Err(()) => return iso_err(valid_to.as_deref().unwrap_or("")),
            };
            run_safely(move |c| {
                set_price(
                    c,
                    offering,
                    parsed_amount,
                    currency,
                    vf,
                    vt,
                    price_id,
                    retire_current,
                )
            })
            .await
        }
        AdminCatalogCommand::WindowOffering {
            offering_id,
            valid_from,
            valid_to,
        } => {
            let vf = match opt_iso(&valid_from) {
                Ok(v) => v,
                Err(()) => return iso_err(valid_from.as_deref().unwrap_or("")),
            };
            let vt = match opt_iso(&valid_to) {
                Ok(v) => v,
                Err(()) => return iso_err(valid_to.as_deref().unwrap_or("")),
            };
            run_safely(move |c| window_offering(c, offering_id, valid_from, valid_to, vf, vt)).await
        }
        AdminCatalogCommand::RetireOffering { offering_id } => {
            run_safely(move |c| retire_offering(c, offering_id)).await
        }
        AdminCatalogCommand::MigratePrice {
            offering,
            new_price_id,
            effective_from,
            notice_days,
            initiated_by,
        } => {
            let ef = match parse_iso(&effective_from) {
                Ok(v) => v,
                Err(()) => return iso_err(&effective_from),
            };
            run_safely(move |c| {
                migrate_price(c, offering, new_price_id, ef, notice_days, initiated_by)
            })
            .await
        }
        AdminCatalogCommand::Show { at } => {
            let moment = match &at {
                Some(raw) => match parse_iso(raw) {
                    Ok(v) => v,
                    Err(()) => return iso_err(raw),
                },
                // Python defaults to `clock_now()`; the client + title share the moment.
                None => bss_clock::now().format("%Y-%m-%dT%H:%M:%S%:z").to_string(),
            };
            run_safely(move |c| show(c, moment)).await
        }
    }
}

/// `str(Decimal(x))` failed — Python raises an uncaught `InvalidOperation` → exit 1.
fn decimal_err(raw: &str) -> ExitCode {
    eprintln!("invalid decimal: '{raw}'");
    ExitCode::from(1)
}

/// `_parse_iso` rejected the value — Python prints + `Exit(2)`.
fn iso_err(raw: &str) -> ExitCode {
    eprintln!("Invalid ISO-8601 datetime: '{raw}'");
    ExitCode::from(2)
}

/// `_parse_iso` over an optional argument — `None` passes through, `Some` validates.
fn opt_iso(value: &Option<String>) -> Result<Option<String>, ()> {
    match value {
        None => Ok(None),
        Some(v) => parse_iso(v).map(Some),
    }
}

#[allow(clippy::too_many_arguments)]
async fn add_offering(
    c: Arc<Clients>,
    offering_id: String,
    name: String,
    amount: String,
    currency: String,
    valid_from: Option<String>,
    valid_to: Option<String>,
    data_mb: Option<i64>,
    voice_min: Option<i64>,
    sms_count: Option<i64>,
    data_roaming_mb: Option<i64>,
) -> Result<(), ClientError> {
    let result = c
        .catalog
        .admin_add_offering(
            &offering_id,
            &name,
            &amount,
            &currency,
            // Python's `spec_id` default; the CLI never overrides it.
            "SPEC_MOBILE_PREPAID",
            valid_from.as_deref(),
            valid_to.as_deref(),
            data_mb,
            voice_min,
            sms_count,
            data_roaming_mb,
        )
        .await?;
    let id = result.get("id").and_then(Value::as_str).unwrap_or("");
    let name = result.get("name").and_then(Value::as_str).unwrap_or("");
    println!("✓ Added offering {id} — {name}");
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn set_price(
    c: Arc<Clients>,
    offering: String,
    amount: String,
    currency: String,
    valid_from: String,
    valid_to: Option<String>,
    price_id: Option<String>,
    retire_current: bool,
) -> Result<(), ClientError> {
    // `price_id or f"PRICE_{offering}_{int(clock_now().timestamp())}"`.
    let resolved_id = price_id.unwrap_or_else(|| {
        let ts = bss_clock::now().timestamp();
        format!("PRICE_{offering}_{ts}")
    });
    let result = c
        .catalog
        .admin_add_price(
            &offering,
            &resolved_id,
            &amount,
            &currency,
            Some(valid_from.as_str()),
            valid_to.as_deref(),
            retire_current,
        )
        .await?;
    let id = result.get("id").and_then(Value::as_str).unwrap_or("");
    println!("✓ Added price {id} for {offering}: {currency} {amount}");
    Ok(())
}

async fn window_offering(
    c: Arc<Clients>,
    offering_id: String,
    raw_from: Option<String>,
    raw_to: Option<String>,
    valid_from: Option<String>,
    valid_to: Option<String>,
) -> Result<(), ClientError> {
    let result = c
        .catalog
        .admin_set_offering_window(&offering_id, valid_from.as_deref(), valid_to.as_deref())
        .await?;
    let id = result.get("id").and_then(Value::as_str).unwrap_or("");
    // Python echoes the RAW input strings (`valid_from or 'NULL'`), not the parsed form.
    let from = raw_from
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "NULL".to_string());
    let to = raw_to
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "NULL".to_string());
    println!("✓ Windowed {id}: valid_from={from}, valid_to={to}");
    Ok(())
}

async fn retire_offering(c: Arc<Clients>, offering_id: String) -> Result<(), ClientError> {
    let result = c.catalog.admin_retire_offering(&offering_id).await?;
    let id = result.get("id").and_then(Value::as_str).unwrap_or("");
    let name = result.get("name").and_then(Value::as_str).unwrap_or("");
    println!("✓ Retired {id} — {name}");
    Ok(())
}

async fn migrate_price(
    c: Arc<Clients>,
    offering: String,
    new_price_id: String,
    effective_from: String,
    notice_days: i64,
    initiated_by: String,
) -> Result<(), ClientError> {
    let result = c
        .subscription
        .migrate_to_new_price(
            &offering,
            &new_price_id,
            &effective_from,
            notice_days,
            &initiated_by,
        )
        .await?;
    let count = result.get("count").and_then(Value::as_i64).unwrap_or(0);
    println!("✓ Scheduled price migration: {count} subscription(s) on {offering}");
    if let Some(ids) = result.get("subscriptionIds").and_then(Value::as_array) {
        for sub_id in ids {
            if let Some(sub_id) = sub_id.as_str() {
                println!("  • {sub_id}");
            }
        }
    }
    Ok(())
}

async fn show(c: Arc<Clients>, moment: String) -> Result<(), ClientError> {
    let offerings = c.catalog.list_active_offerings(&moment).await?;
    // Python renders a `rich.Table`; box-drawing chrome is a documented CLI seam. The
    // per-offering values (recurring-price pick, `... or 'NULL'`) match Python exactly.
    println!("Active catalog @ {moment}");
    println!("offering  name  price  valid_from  valid_to");
    let empty = Vec::new();
    for o in offerings.as_array().unwrap_or(&empty) {
        let id = o.get("id").and_then(Value::as_str).unwrap_or("");
        let name = o.get("name").and_then(Value::as_str).unwrap_or("");
        let price_str = recurring_price(o);
        let valid_for = o.get("validFor");
        let start = or_null(valid_for.and_then(|v| v.get("startDateTime")));
        let end = or_null(valid_for.and_then(|v| v.get("endDateTime")));
        println!("{id}  {name}  {price_str}  {start}  {end}");
    }
    Ok(())
}

/// The recurring price as `f"{unit} {value}"`, or `—` when there's no recurring row.
fn recurring_price(o: &Value) -> String {
    let prices = o.get("productOfferingPrice").and_then(Value::as_array);
    let recurring = prices.and_then(|ps| {
        ps.iter()
            .find(|p| p.get("priceType").and_then(Value::as_str) == Some("recurring"))
    });
    match recurring {
        None => "—".to_string(),
        Some(r) => {
            let tx = r.get("price").and_then(|p| p.get("taxIncludedAmount"));
            let unit = tx
                .and_then(|t| t.get("unit"))
                .and_then(Value::as_str)
                .unwrap_or("SGD");
            let value = tx
                .and_then(|t| t.get("value"))
                .map(scalar)
                .unwrap_or_else(|| "?".to_string());
            format!("{unit} {value}")
        }
    }
}

/// Python `str(x or "NULL")` for a datetime field — falsy/absent ⇒ `NULL`.
fn or_null(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => "NULL".to_string(),
    }
}

/// A JSON scalar as an f-string renders it (bare number/string).
fn scalar(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".to_string(),
        other => other.to_string(),
    }
}
