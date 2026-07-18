//! `bss trace ...` — Jaeger trace lookup + ASCII swimlane (v0.2). Port of
//! `cli/bss_cli/commands/trace.py`. Resolves a trace either directly (by id, or the
//! latest `bss.ask`) or via the audit-event trail for an order/subscription, then
//! renders the swimlane (or raw Jaeger JSON with `--json`).

use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::{AuditClient, JaegerClient, JaegerError, TokenAuthProvider};
use bss_cockpit::renderers::trace::{render_swimlane, SwimlaneOpts};
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::cli_ctx;

#[derive(Args)]
pub struct TraceArgs {
    #[command(subcommand)]
    command: TraceCommand,
}

#[derive(Subcommand)]
enum TraceCommand {
    /// Render the swimlane for a trace ID.
    Get {
        /// 32-char hex trace ID.
        trace_id: String,
        /// Override terminal width.
        #[arg(long)]
        width: Option<usize>,
        /// Include SQL spans.
        #[arg(long = "show-sql")]
        show_sql: bool,
        /// Filter to one service.
        #[arg(long = "service")]
        only_service: Option<String>,
        /// Emit raw Jaeger JSON.
        #[arg(long)]
        json: bool,
    },
    /// Resolve trace_id from audit events for an order, then render.
    ForOrder {
        /// Commercial order ID, e.g. ORD-014.
        order_id: String,
        #[arg(long)]
        width: Option<usize>,
        #[arg(long = "show-sql")]
        show_sql: bool,
        #[arg(long)]
        json: bool,
    },
    /// Resolve trace_id from audit events for a subscription, then render.
    ForSubscription {
        /// Subscription ID, e.g. SUB-007.
        subscription_id: String,
        #[arg(long)]
        width: Option<usize>,
        #[arg(long = "show-sql")]
        show_sql: bool,
        #[arg(long)]
        json: bool,
    },
    /// Render the most-recent `bss ask` trace.
    ForAsk {
        #[arg(long)]
        width: Option<usize>,
        #[arg(long = "show-sql")]
        show_sql: bool,
        #[arg(long)]
        json: bool,
    },
    /// List services currently exporting traces to Jaeger.
    Services,
}

pub async fn run(args: TraceArgs) -> ExitCode {
    // The audit reads carry the CLI actor/channel; the Jaeger API is unauthenticated
    // but scoping the whole command is harmless and consistent with the other groups.
    bss_context::scope(cli_ctx(), dispatch(args)).await
}

async fn dispatch(args: TraceArgs) -> ExitCode {
    match args.command {
        TraceCommand::Get {
            trace_id,
            width,
            show_sql,
            only_service,
            json,
        } => {
            let jc = match JaegerClient::from_env() {
                Ok(jc) => jc,
                Err(e) => return jaeger_err(&e),
            };
            let trace = match jc.get_trace(&trace_id).await {
                Ok(t) => t,
                Err(e) => return jaeger_err(&e),
            };
            emit(&trace, json, width, show_sql, only_service.as_deref());
            ExitCode::SUCCESS
        }
        TraceCommand::ForOrder {
            order_id,
            width,
            show_sql,
            json,
        } => {
            let url = service_url("BSS_COM_URL", "http://com:8000");
            trace_from_audit(
                &url,
                "ProductOrder",
                &order_id,
                "order",
                width,
                show_sql,
                json,
            )
            .await
        }
        TraceCommand::ForSubscription {
            subscription_id,
            width,
            show_sql,
            json,
        } => {
            let url = service_url("BSS_SUBSCRIPTION_URL", "http://subscription:8000");
            trace_from_audit(
                &url,
                "subscription",
                &subscription_id,
                "subscription",
                width,
                show_sql,
                json,
            )
            .await
        }
        TraceCommand::ForAsk {
            width,
            show_sql,
            json,
        } => {
            let jc = match JaegerClient::from_env() {
                Ok(jc) => jc,
                Err(e) => return jaeger_err(&e),
            };
            let tid = match jc.latest_ask_trace_id().await {
                Ok(Some(t)) => t,
                Ok(None) => {
                    println!("no recent bss.ask trace found in Jaeger");
                    return ExitCode::from(2);
                }
                Err(e) => return jaeger_err(&e),
            };
            let trace = match jc.get_trace(&tid).await {
                Ok(t) => t,
                Err(e) => return jaeger_err(&e),
            };
            emit(&trace, json, width, show_sql, None);
            ExitCode::SUCCESS
        }
        TraceCommand::Services => {
            let jc = match JaegerClient::from_env() {
                Ok(jc) => jc,
                Err(e) => return jaeger_err(&e),
            };
            match jc.list_services().await {
                Ok(mut names) => {
                    names.sort();
                    for name in names {
                        println!("{name}");
                    }
                    ExitCode::SUCCESS
                }
                Err(e) => jaeger_err(&e),
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn trace_from_audit(
    audit_url: &str,
    aggregate_type: &str,
    aggregate_id: &str,
    noun: &str,
    width: Option<usize>,
    show_sql: bool,
    json: bool,
) -> ExitCode {
    let token = std::env::var("BSS_API_TOKEN").unwrap_or_default();
    let auth = match TokenAuthProvider::new(token) {
        Ok(a) => Arc::new(a),
        Err(e) => {
            eprintln!("client setup failed: {e}");
            return ExitCode::from(1);
        }
    };
    let ac = match AuditClient::new(audit_url.to_string(), auth) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("client setup failed: {e}");
            return ExitCode::from(1);
        }
    };
    let events = match ac
        .list_events(Some(aggregate_type), Some(aggregate_id), 20)
        .await
    {
        Ok(e) => e,
        Err(e) => {
            eprintln!("error: {e}");
            return ExitCode::from(1);
        }
    };
    let Some(trace_id) = latest_trace_id(&events) else {
        println!("no trace_id recorded for {noun} {aggregate_id} (was it created before v0.2?)");
        return ExitCode::from(2);
    };
    let jc = match JaegerClient::from_env() {
        Ok(jc) => jc,
        Err(e) => return jaeger_err(&e),
    };
    let trace = match jc.get_trace(&trace_id).await {
        Ok(t) => t,
        Err(e) => return jaeger_err(&e),
    };
    emit(&trace, json, width, show_sql, None);
    ExitCode::SUCCESS
}

/// Print either the raw Jaeger JSON (`--json`) or the rendered swimlane.
fn emit(
    trace: &Value,
    as_json: bool,
    width: Option<usize>,
    show_sql: bool,
    only_service: Option<&str>,
) {
    if as_json {
        println!(
            "{}",
            serde_json::to_string_pretty(trace).unwrap_or_else(|_| trace.to_string())
        );
        return;
    }
    let opts = SwimlaneOpts {
        width,
        show_sql,
        only_service,
    };
    println!("{}", render_swimlane(trace, &opts));
}

/// The `traceId`/`trace_id` of the most recent event with one set. Audit events come
/// back ascending by `occurredAt`, so we walk from the end (the completion trace, with
/// the manual span fan-out, is more interesting than the initial create).
fn latest_trace_id(events: &Value) -> Option<String> {
    let arr = events.as_array()?;
    for ev in arr.iter().rev() {
        let tid = ev
            .get("traceId")
            .and_then(Value::as_str)
            .or_else(|| ev.get("trace_id").and_then(Value::as_str));
        if let Some(tid) = tid.filter(|s| !s.is_empty()) {
            return Some(tid.to_string());
        }
    }
    None
}

fn jaeger_err(e: &JaegerError) -> ExitCode {
    eprintln!("{e}");
    ExitCode::from(2)
}

/// A downstream service URL from env (mirrors `bss_orchestrator.config.settings`).
fn service_url(var: &str, default: &str) -> String {
    std::env::var(var)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}
