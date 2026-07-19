//! `bss payment ...` — payment-method + charge commands. Port of
//! `cli/bss_cli/commands/payment.py`.

use std::io::Write as _;
use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::ClientError;
use bss_orchestrator::tools::payment::local_tokenize_card;
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::{run_safely, run_safely_code, Clients};

#[derive(Args)]
pub struct PaymentArgs {
    #[command(subcommand)]
    command: PaymentCommand,
}

#[derive(Subcommand)]
enum PaymentCommand {
    /// Tokenise a PAN client-side and attach it as a payment method (dev-only,
    /// requires BSS_PAYMENT_PROVIDER=mock).
    AddCard {
        #[arg(long)]
        customer: String,
        /// 16-digit PAN (tokenised by CLI).
        #[arg(long)]
        card: String,
    },
    /// List payment methods for a customer.
    ListMethods {
        #[arg(long)]
        customer: String,
    },
    /// Remove a payment method (destructive).
    RemoveMethod {
        method_id: String,
        #[arg(long = "allow-destructive")]
        allow_destructive: bool,
    },
    /// v0.16 cutover — invalidate mock-token payment methods before a real-provider
    /// switch.
    Cutover {
        /// Mark every active mock-token payment method as expired.
        #[arg(long = "invalidate-mock-tokens")]
        invalidate_mock_tokens: bool,
        /// Print the count without writing.
        #[arg(long = "dry-run")]
        dry_run: bool,
        /// Skip the y/N prompt (for scripts).
        #[arg(long, short = 'y')]
        yes: bool,
    },
}

pub async fn run(args: PaymentArgs) -> ExitCode {
    match args.command {
        PaymentCommand::AddCard { customer, card } => {
            // v0.16+ — server-side tokenization is dev-only; stripe mode adds cards
            // via the portal. The provider gate is a pure pre-flight check.
            let provider =
                std::env::var("BSS_PAYMENT_PROVIDER").unwrap_or_else(|_| "mock".to_string());
            if provider != "mock" {
                eprintln!(
                    "ERROR bss payment add-card is dev-only and requires \
                     BSS_PAYMENT_PROVIDER=mock (currently '{provider}')."
                );
                eprintln!(
                    "In stripe mode, customers add cards via the self-serve portal \
                     (Stripe Elements)."
                );
                eprintln!(
                    "For test data, use the portal with Stripe test cards \
                     (e.g. 4242 4242 4242 4242)."
                );
                return ExitCode::from(2);
            }
            run_safely_code(move |c| add_card(c, customer, card)).await
        }
        PaymentCommand::ListMethods { customer } => {
            run_safely(move |c| list_methods(c, customer)).await
        }
        PaymentCommand::RemoveMethod {
            method_id,
            allow_destructive,
        } => {
            if !allow_destructive {
                eprintln!("remove-method is gated behind --allow-destructive.");
                return ExitCode::from(2);
            }
            run_safely(move |c| remove_method(c, method_id)).await
        }
        PaymentCommand::Cutover {
            invalidate_mock_tokens,
            dry_run,
            yes,
        } => {
            if !invalidate_mock_tokens {
                println!(
                    "nothing to do — pass --invalidate-mock-tokens to run the cutover \
                     (or --dry-run to preview)."
                );
                return ExitCode::SUCCESS;
            }
            run_safely_code(move |c| cutover(c, dry_run, yes)).await
        }
    }
}

async fn add_card(
    c: Arc<Clients>,
    customer: String,
    card: String,
) -> Result<ExitCode, ClientError> {
    // The tokenizer is a pure sandbox port owned by the orchestrator (same import
    // the Python CLI uses). A bad PAN is a ValueError → exit 1, matching Python's
    // uncaught ValueError (which `_run_safely` doesn't catch).
    let (token, last4, brand) = match local_tokenize_card(&card) {
        Ok(t) => t,
        Err(detail) => {
            eprintln!("{detail}");
            return Ok(ExitCode::from(1));
        }
    };
    let pm = c
        .payment
        .create_payment_method(&customer, &token, &last4, &brand, 12, 2030)
        .await?;
    let id = pm.get("id").and_then(Value::as_str).unwrap_or("");
    let cs = pm.get("cardSummary");
    let br = cs
        .and_then(|c| c.get("brand"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let l4 = cs
        .and_then(|c| c.get("last4"))
        .and_then(Value::as_str)
        .unwrap_or("");
    println!("Added {id}  {br}•••{l4}");
    Ok(ExitCode::SUCCESS)
}

async fn list_methods(c: Arc<Clients>, customer: String) -> Result<(), ClientError> {
    let methods = c.payment.list_methods(&customer).await?;
    let empty = Vec::new();
    for pm in methods.as_array().unwrap_or(&empty) {
        let id = pm.get("id").and_then(Value::as_str).unwrap_or("");
        let cs = pm.get("cardSummary");
        let brand = cs
            .and_then(|c| c.get("brand"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let last4 = cs
            .and_then(|c| c.get("last4"))
            .and_then(Value::as_str)
            .unwrap_or("");
        // Python `f"{exp_m:02}/{exp_y}" if exp_m and exp_y else "  /    "` — a zero
        // or missing month/year falls back to the fixed-width blank.
        let exp_m = cs.and_then(|c| c.get("expMonth")).and_then(Value::as_i64);
        let exp_y = cs.and_then(|c| c.get("expYear")).and_then(Value::as_i64);
        let exp = match (exp_m, exp_y) {
            (Some(m), Some(y)) if m != 0 && y != 0 => format!("{m:02}/{y}"),
            _ => "  /    ".to_string(),
        };
        println!("{id:<9} {brand:<6}•••{last4}  {exp}");
    }
    Ok(())
}

async fn remove_method(c: Arc<Clients>, method_id: String) -> Result<(), ClientError> {
    let out = c.payment.remove_method(&method_id).await?;
    // Python `out.get('id')` → the literal "None" when absent.
    let id = out
        .get("id")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| "None".to_string());
    println!("Removed {id}");
    Ok(())
}

async fn cutover(c: Arc<Clients>, dry_run: bool, yes: bool) -> Result<ExitCode, ClientError> {
    // Always preview first so the operator sees the scope before writing.
    let preview = c.payment.cutover_invalidate_mock_tokens(true).await?;
    let count = preview
        .get("candidate_count")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if count == 0 {
        println!("No mock-token payment methods to invalidate.");
        return Ok(ExitCode::SUCCESS);
    }
    println!("Found {count} active payment_methods with token_provider='mock'.");
    if dry_run {
        println!("Dry run — no writes performed.");
        if let Some(ids) = preview.get("candidate_ids").and_then(Value::as_array) {
            for pm_id in ids {
                if let Some(pm_id) = pm_id.as_str() {
                    println!("  would invalidate: {pm_id}");
                }
            }
        }
        return Ok(ExitCode::SUCCESS);
    }
    if !yes && !confirm_prompt() {
        println!("Aborted.");
        return Ok(ExitCode::from(2));
    }
    let result = c.payment.cutover_invalidate_mock_tokens(false).await?;
    let n = result
        .get("invalidated_count")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    println!("Invalidated {n} payment methods.");
    println!(
        "Each row emitted a payment_method.cutover_invalidated event for the \
         email-template flow to pick up."
    );
    Ok(ExitCode::SUCCESS)
}

/// Mirror `typer.confirm(..., default=False)` — prompt `... [y/N]:` and treat only an
/// explicit y/yes (case-insensitive) as confirmation; empty/EOF is the `False` default.
fn confirm_prompt() -> bool {
    print!(
        "Mark all as expired? Customers will see 'please update your payment method' \
         on next attempt. [y/N]: "
    );
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    if std::io::stdin().read_line(&mut line).is_err() {
        return false;
    }
    matches!(line.trim().to_ascii_lowercase().as_str(), "y" | "yes")
}
