//! `bss promo ...` — operator promotion management (v1.1). Port of
//! `cli/bss_cli/commands/promo.py`. Thin wrappers over the catalog promo surface
//! (which composes over loyalty-cli). Operator-only — NOT in `customer_self_serve`.

use std::process::ExitCode;
use std::str::FromStr;
use std::sync::Arc;

use bss_clients::ClientError;
use clap::{Args, Subcommand};
use rust_decimal::Decimal;
use serde_json::Value;

use crate::runtime::{run_safely_promo, Clients};

#[derive(Args)]
pub struct PromoArgs {
    #[command(subcommand)]
    command: PromoCommand,
}

#[derive(Subcommand)]
// `Create` carries every promo field flat (a clap-derived variant — boxing the
// fields would fight the `#[arg]` derive); the size gap to the other variants is
// benign for a short-lived CLI parse.
#[allow(clippy::large_enum_variant)]
enum PromoCommand {
    /// Create a promotion (BSS money terms + loyalty entitlement saga).
    Create {
        /// Promotion id, e.g. PROMO_SUMMER25.
        #[arg(long = "id")]
        promotion_id: String,
        /// percent | absolute.
        #[arg(long = "type")]
        discount_type: String,
        /// Discount amount (percent 0-100, or absolute).
        #[arg(long = "value")]
        discount_value: String,
        /// single | multi | perpetual.
        #[arg(long = "duration")]
        duration_kind: String,
        /// public (typed) | targeted (eligibility-gated, auto-applied).
        #[arg(long, default_value = "public")]
        audience: String,
        #[arg(long, default_value = "SGD")]
        currency: String,
        /// Typed code (non-targeted). Omit for codeless targeted.
        #[arg(long)]
        code: Option<String>,
        /// single_use_shared | multi_use | single_use_unique_per_customer.
        #[arg(long = "code-kind")]
        promo_code_kind: Option<String>,
        /// Comma-separated offering ids to restrict to. Omit = all sellable.
        #[arg(long)]
        offerings: Option<String>,
        /// Number of periods (required for --duration multi, >= 2).
        #[arg(long = "periods")]
        periods_total: Option<i64>,
        #[arg(long = "valid-from")]
        valid_from: Option<String>,
        #[arg(long = "valid-to")]
        valid_to: Option<String>,
        #[arg(long = "name")]
        display_name: Option<String>,
    },
    /// Add customers to a targeted promotion's eligibility list.
    Assign {
        /// An active promotion id.
        #[arg(long = "promo")]
        promotion_id: String,
        /// Comma-separated customer ids.
        #[arg(long)]
        customers: String,
    },
    /// Remove customers from a targeted promotion's eligibility list (v1.3.1).
    Unassign {
        /// An active targeted promotion id.
        #[arg(long = "promo")]
        promotion_id: String,
        /// Comma-separated customer ids.
        #[arg(long)]
        customers: String,
    },
    /// Terminal-stop a promotion (v1.4.1): active → exhausted.
    Exhaust { promotion_id: String },
    /// Show a promotion's money terms, loyalty link, and state.
    Show { promotion_id: String },
}

pub async fn run(args: PromoArgs) -> ExitCode {
    match args.command {
        PromoCommand::Create {
            promotion_id,
            discount_type,
            discount_value,
            duration_kind,
            audience,
            currency,
            code,
            promo_code_kind,
            offerings,
            periods_total,
            valid_from,
            valid_to,
            display_name,
        } => {
            // Python `value = str(Decimal(discount_value))` runs before any async work;
            // an invalid decimal is an uncaught InvalidOperation → exit 1.
            let value = match normalize_decimal(&discount_value) {
                Some(v) => v,
                None => {
                    eprintln!("invalid decimal: '{discount_value}'");
                    return ExitCode::from(1);
                }
            };
            // Python `[o.strip() for o in offerings.split(",")]` — no empty filter here.
            let offering_ids: Option<Vec<String>> =
                offerings.map(|o| o.split(',').map(|s| s.trim().to_string()).collect());
            run_safely_promo(move |c| async move {
                create(
                    c,
                    promotion_id,
                    discount_type,
                    value,
                    duration_kind,
                    audience,
                    currency,
                    code,
                    promo_code_kind,
                    offering_ids,
                    periods_total,
                    valid_from,
                    valid_to,
                    display_name,
                )
                .await
            })
            .await
        }
        PromoCommand::Assign {
            promotion_id,
            customers,
        } => run_safely_promo(move |c| assign(c, promotion_id, customers)).await,
        PromoCommand::Unassign {
            promotion_id,
            customers,
        } => run_safely_promo(move |c| unassign(c, promotion_id, customers)).await,
        PromoCommand::Exhaust { promotion_id } => {
            run_safely_promo(move |c| exhaust(c, promotion_id)).await
        }
        PromoCommand::Show { promotion_id } => {
            run_safely_promo(move |c| show(c, promotion_id)).await
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn create(
    c: Arc<Clients>,
    promotion_id: String,
    discount_type: String,
    discount_value: String,
    duration_kind: String,
    audience: String,
    currency: String,
    code: Option<String>,
    promo_code_kind: Option<String>,
    offering_ids: Option<Vec<String>>,
    periods_total: Option<i64>,
    valid_from: Option<String>,
    valid_to: Option<String>,
    display_name: Option<String>,
) -> Result<(), ClientError> {
    let result = c
        .catalog
        .create_promotion(
            &promotion_id,
            &discount_type,
            &discount_value,
            &duration_kind,
            &audience,
            &currency,
            code.as_deref(),
            promo_code_kind.as_deref(),
            offering_ids.as_deref(),
            periods_total,
            valid_from.as_deref(),
            valid_to.as_deref(),
            display_name.as_deref(),
        )
        .await?;
    let id = result.get("id").and_then(Value::as_str).unwrap_or("");
    let audience = get_or_none(&result, "audience");
    let code = get_or_none(&result, "code");
    let state = result.get("state").and_then(Value::as_str).unwrap_or("");
    let od = get_or_none(&result, "offerDefinitionId");
    println!(
        "✓ Created promotion {id} (audience={audience}, code={code}) — \
         state={state}, OD={od}"
    );
    Ok(())
}

async fn assign(
    c: Arc<Clients>,
    promotion_id: String,
    customers: String,
) -> Result<(), ClientError> {
    let customer_ids = split_customers(&customers);
    let result = c
        .catalog
        .assign_promotion(&promotion_id, &customer_ids)
        .await?;
    let eligible = string_list(&result, "eligible");
    let already = string_list(&result, "already");
    let code = get_or_none(&result, "code");
    println!(
        "✓ Eligibility for {promotion_id} (code {code}): {} added, {} already",
        eligible.len(),
        already.len()
    );
    for cid in &eligible {
        println!("  • added {cid}");
    }
    for cid in &already {
        println!("  • already {cid}");
    }
    Ok(())
}

async fn unassign(
    c: Arc<Clients>,
    promotion_id: String,
    customers: String,
) -> Result<(), ClientError> {
    let customer_ids = split_customers(&customers);
    let result = c
        .catalog
        .unassign_promotion(&promotion_id, &customer_ids)
        .await?;
    let removed = string_list(&result, "removed");
    let not_eligible = string_list(&result, "not_eligible");
    let code = get_or_none(&result, "code");
    println!(
        "✓ Removed from {promotion_id} (code {code}): {} removed, {} not eligible",
        removed.len(),
        not_eligible.len()
    );
    for cid in &removed {
        println!("  • removed {cid}");
    }
    for cid in &not_eligible {
        println!("  • not_eligible {cid}");
    }
    Ok(())
}

async fn exhaust(c: Arc<Clients>, promotion_id: String) -> Result<(), ClientError> {
    let promo = c.catalog.exhaust_promotion(&promotion_id).await?;
    let state = promo.get("state").and_then(Value::as_str).unwrap_or("?");
    if state == "exhausted" {
        println!(
            "✓ {promotion_id} is now exhausted (was active). New orders will see no discount."
        );
    } else {
        println!("! {promotion_id} state: {state} (unexpected; check `bss promo show`)");
    }
    Ok(())
}

async fn show(c: Arc<Clients>, promotion_id: String) -> Result<(), ClientError> {
    let p = c.catalog.get_promotion(&promotion_id).await?;
    let id = p.get("id").and_then(Value::as_str).unwrap_or("");
    // Python renders a `rich.Table`; the box-drawing chrome isn't byte-reproduced
    // (documented CLI seam, same as `bss inventory`). The field VALUES match Python's
    // `str(... or "—")` / f-string composition exactly.
    let discount = format!(
        "{} {}",
        py_str(p.get("discountType")),
        py_str(p.get("discountValue"))
    );
    let duration = format!(
        "{} (periods={})",
        py_str(p.get("durationKind")),
        or_dash(p.get("periodsTotal"))
    );
    let rows: [(&str, String); 10] = [
        ("name", or_dash(p.get("name"))),
        (
            "state",
            p.get("state")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        ),
        ("audience", or_dash(p.get("audience"))),
        ("code", or_dash(p.get("code"))),
        ("offerDefinitionId", or_dash(p.get("offerDefinitionId"))),
        ("discount", discount),
        ("duration", duration),
        (
            "applicableOfferings",
            or_default(p.get("applicableOfferingIds"), "all"),
        ),
        ("validFrom", or_dash(p.get("validFrom"))),
        ("validTo", or_dash(p.get("validTo"))),
    ];
    println!("Promotion {id}");
    for (field, value) in &rows {
        println!("  {field:<20} {value}");
    }
    Ok(())
}

/// `str(Decimal(s))` — validate + canonicalise. `rust_decimal`'s round-trip preserves
/// scale (no trailing-zero stripping) the same way CPython's `Decimal.__str__` does for
/// plain decimals; leading whitespace/zeros are absorbed. `None` ⇒ Python's
/// `InvalidOperation`.
fn normalize_decimal(s: &str) -> Option<String> {
    Decimal::from_str(s.trim()).ok().map(|d| d.to_string())
}

/// `[c.strip() for c in customers.split(",") if c.strip()]` — trim, drop empties.
fn split_customers(customers: &str) -> Vec<String> {
    customers
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

/// A response array field as `Vec<String>` (skips non-string entries).
fn string_list(v: &Value, key: &str) -> Vec<String> {
    v.get(key)
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|x| x.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default()
}

/// `result.get(key)` rendered as an f-string would — the literal `None` when absent.
fn get_or_none(v: &Value, key: &str) -> String {
    match v.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => "None".to_string(),
        Some(other) => py_str(Some(other)),
    }
}

/// Python `str(p.get(key) or "—")` — falsy (absent/null/empty/zero) ⇒ the em-dash.
fn or_dash(v: Option<&Value>) -> String {
    or_default(v, "—")
}

/// Python `str(p.get(key) or fallback)` with an explicit fallback.
fn or_default(v: Option<&Value>, fallback: &str) -> String {
    match v {
        Some(val) if truthy(val) => py_str(Some(val)),
        _ => fallback.to_string(),
    }
}

/// Python truthiness for a JSON value: null/false/0/""/[]/{} are falsy.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `str(value)` as CPython renders it for the shapes promo `show` produces: a bare
/// string, a `['a', 'b']` list repr, `None` for null, else the compact JSON.
fn py_str(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => if *b { "True" } else { "False" }.to_string(),
        Some(Value::Array(a)) => {
            let items: Vec<String> = a
                .iter()
                .map(|x| match x {
                    Value::String(s) => format!("'{s}'"),
                    other => py_str(Some(other)),
                })
                .collect();
            format!("[{}]", items.join(", "))
        }
        Some(other) => other.to_string(),
    }
}
