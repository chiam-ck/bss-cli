//! `bss admin ...` — operator-only tools. Port of `cli/bss_cli/commands/admin.py`.
//!
//! The Python `admin` app mounts three things: `catalog` (offering/price management),
//! `reset` (the cross-service operational-data wipe), and `knowledge` (FTS
//! reindex/search over `bss-knowledge`). This module wires all three; the leaf logic
//! lives in `admin_catalog` / `admin_knowledge`.

use std::io::Write as _;
use std::process::ExitCode;
use std::sync::Arc;

use bss_clients::{AdminClient, ClientError, TokenAuthProvider};
use clap::{Args, Subcommand};
use serde_json::Value;

use crate::runtime::cli_ctx;

#[derive(Args)]
pub struct AdminArgs {
    #[command(subcommand)]
    command: AdminCommand,
}

#[derive(Subcommand)]
// `Catalog` wraps the large clap-derived AdminCatalogArgs; boxing it would fight the
// derive, and the size gap is benign for a one-shot CLI parse.
#[allow(clippy::large_enum_variant)]
enum AdminCommand {
    /// Operator catalog management (offerings, prices, windows, migrations).
    Catalog(super::admin_catalog::AdminCatalogArgs),
    /// Doc-corpus indexer + FTS search debug surface (v0.20+).
    Knowledge(super::admin_knowledge::KnowledgeArgs),
    /// Seed reference data across every domain (idempotent — the clean reseed).
    Seed,
    /// Seed the synced demo dataset (3 customers + 2 promos + VIP assign), BSS +
    /// loyalty in lockstep. `--reset` surgically reverses it (demo-prefix only).
    SeedDemo {
        /// Remove everything `seed-demo` creates (demo-prefix only; spares operator data).
        #[arg(long)]
        reset: bool,
    },
    /// Apply the sqlx schema migrations (Phase 8 — Alembic freeze → sqlx baseline).
    Migrate {
        /// Existing install: stamp the baseline as applied WITHOUT running its SQL
        /// (the schema already exists from Alembic). Fresh installs omit this.
        #[arg(long)]
        baseline: bool,
    },
    /// Wipe operational data across every BSS service (reference data survives).
    Reset {
        /// Skip interactive confirmation.
        #[arg(long, short = 'y')]
        yes: bool,
    },
}

pub async fn run(args: AdminArgs) -> ExitCode {
    match args.command {
        AdminCommand::Catalog(a) => super::admin_catalog::run(a).await,
        AdminCommand::Knowledge(a) => super::admin_knowledge::run(a).await,
        AdminCommand::Seed => super::admin_seed::run().await,
        AdminCommand::SeedDemo { reset } => super::admin_seed_demo::run(reset).await,
        AdminCommand::Migrate { baseline } => super::admin_migrate::run(baseline).await,
        AdminCommand::Reset { yes } => reset(yes).await,
    }
}

/// A service that owns operational data: display label + base-URL env var + default.
const TARGETS: &[(&str, &str, &str)] = &[
    ("mediation", "BSS_MEDIATION_URL", "http://localhost:8007"),
    (
        "subscription",
        "BSS_SUBSCRIPTION_URL",
        "http://localhost:8006",
    ),
    ("som", "BSS_SOM_URL", "http://localhost:8005"),
    ("com", "BSS_COM_URL", "http://localhost:8004"),
    (
        "provisioning-sim",
        "BSS_PROVISIONING_URL",
        "http://localhost:8010",
    ),
    ("payment", "BSS_PAYMENT_URL", "http://localhost:8003"),
    ("crm (+ inventory)", "BSS_CRM_URL", "http://localhost:8002"),
];

struct ResetResult {
    label: &'static str,
    status: &'static str, // "ok" | "error"
    detail: String,
    response: Option<Value>,
}

async fn reset(yes: bool) -> ExitCode {
    if !yes {
        println!("About to wipe operational data across every BSS service.");
        println!("Reference data (catalog, agents, pools, fault-injection) is preserved.");
        println!("This is irreversible for the current run.");
        if !confirm_reset() {
            println!("Aborted.");
            return ExitCode::from(1);
        }
    }
    let results = bss_context::scope(cli_ctx(), fanout()).await;
    render(&results);
    if results.iter().any(|r| r.status != "ok") {
        return ExitCode::from(2);
    }
    ExitCode::SUCCESS
}

/// `typer.prompt("Type 'reset' to confirm", default="")` — only the literal `reset`
/// (case-insensitive, trimmed) confirms; empty/EOF is the default and aborts.
fn confirm_reset() -> bool {
    print!("Type 'reset' to confirm: ");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    if std::io::stdin().read_line(&mut line).is_err() {
        return false;
    }
    line.trim().eq_ignore_ascii_case("reset")
}

async fn fanout() -> Vec<ResetResult> {
    let token = std::env::var("BSS_API_TOKEN").unwrap_or_default();
    let auth: Arc<dyn bss_clients::AuthProvider> = match TokenAuthProvider::new(token) {
        Ok(a) => Arc::new(a),
        Err(e) => {
            // A bad token can't build any client — surface it against every target.
            let msg = e.to_string();
            return TARGETS
                .iter()
                .map(|(label, _, _)| ResetResult {
                    label,
                    status: "error",
                    detail: msg.clone(),
                    response: None,
                })
                .collect();
        }
    };
    let mut results = Vec::with_capacity(TARGETS.len());
    for (label, var, default) in TARGETS {
        let url = std::env::var(var)
            .ok()
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| default.to_string());
        let result = match AdminClient::new(url, auth.clone()) {
            Ok(client) => match client.reset_operational_data().await {
                Ok(body) => ResetResult {
                    label,
                    status: "ok",
                    detail: "reset".to_string(),
                    response: Some(body),
                },
                Err(e) => ResetResult {
                    label,
                    status: "error",
                    detail: reset_error_detail(&e),
                    response: None,
                },
            },
            Err(e) => ResetResult {
                label,
                status: "error",
                detail: e.to_string(),
                response: None,
            },
        };
        results.push(result);
    }
    results
}

/// Map a client error the way Python's per-exception `except` ladder does.
fn reset_error_detail(e: &ClientError) -> String {
    match e {
        ClientError::NotFound(_) => "404 — admin router not mounted".to_string(),
        ClientError::Timeout(_) => "timeout".to_string(),
        ClientError::Server { detail, .. } => format!("5xx: {detail}"),
        ClientError::Http { status, detail } => format!("{status}: {detail}"),
        ClientError::Policy(p) => format!("422: {p}"),
        ClientError::Transport(d) => format!("transport: {d}"),
    }
}

fn render(results: &[ResetResult]) {
    // Python renders a `rich.Table`; the box-drawing chrome is a documented CLI seam.
    // The per-row cells (schema-count joins, `... or '—'`) match Python exactly.
    println!("bss admin reset");
    println!("service              status  truncated  updated  detail");
    for r in results {
        let (truncated, updated) = schema_counts(r.response.as_ref());
        println!(
            "{:<20} {:<7} {:<10} {:<8} {}",
            r.label, r.status, truncated, updated, r.detail
        );
    }
}

/// `truncated` = `schema(N)` joins over every schema; `updated` = the same but only
/// for schemas that actually updated rows. Each `—` when empty.
fn schema_counts(response: Option<&Value>) -> (String, String) {
    let Some(schemas) = response
        .and_then(|r| r.get("schemas"))
        .and_then(Value::as_array)
    else {
        return ("—".to_string(), "—".to_string());
    };
    let truncated: Vec<String> = schemas
        .iter()
        .map(|s| {
            let name = s.get("schema").and_then(Value::as_str).unwrap_or("");
            let n = s
                .get("truncated")
                .and_then(Value::as_array)
                .map_or(0, Vec::len);
            format!("{name}({n})")
        })
        .collect();
    let updated: Vec<String> = schemas
        .iter()
        .filter_map(|s| {
            let arr = s.get("updated").and_then(Value::as_array)?;
            if arr.is_empty() {
                return None;
            }
            let name = s.get("schema").and_then(Value::as_str).unwrap_or("");
            Some(format!("{name}({})", arr.len()))
        })
        .collect();
    let join_or_dash = |v: Vec<String>| {
        if v.is_empty() {
            "—".to_string()
        } else {
            v.join(", ")
        }
    };
    (join_or_dash(truncated), join_or_dash(updated))
}
