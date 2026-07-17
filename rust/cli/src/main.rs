//! `bss` — the terminal-first, LLM-native cockpit binary. Rust port of the Python
//! Typer app in `cli/bss_cli`.
//!
//! **Slice 1 (this):** the clap root, the `.env` bootstrap (so `bss` works out of
//! the box without a sourced shell), the telemetry root span (the CLI is the root
//! of every `bss <cmd>` trace — without it traces would start at the first BSS
//! service and the `audit.domain_event.trace_id` story for CLI-originated writes
//! would break), and the `clock` command group as the thin-command pattern-setter.
//!
//! **Following slices:** the ~19 client-backed command groups (customer, case,
//! order, …), `bss ask` (single-shot LLM), and the reedline REPL (the canonical
//! operator cockpit — `bss` with no subcommand). `trace.*`/`knowledge.*` land here
//! too, where the registry is built once with the Jaeger/Audit/PgPool handles the
//! P6 portal bundle deliberately omitted.
#![forbid(unsafe_code)]

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

mod commands;
mod runtime;

/// `bss` — BSS-CLI, terminal-first, LLM-native telco BSS.
#[derive(Parser)]
#[command(
    name = "bss",
    version,
    about = "BSS-CLI — terminal-first, LLM-native telco BSS."
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Browse the product catalog (TMF620).
    Catalog(commands::catalog::CatalogArgs),
    /// Time helpers (v0.1 = wall clock).
    Clock(commands::clock::ClockArgs),
    /// Service order + service inventory (SOM).
    Som(commands::som::SomArgs),
    /// Usage simulation (TMF635 mediation).
    Usage(commands::usage::UsageArgs),
}

// A Tokio runtime is required: `init_telemetry`'s OTLP batch exporter runs on
// `rt-tokio`, and the client-backed command groups (following slices) are async.
// The Python CLI is sync + `asyncio.run` per command; here the whole process owns
// one runtime, which is the same single-runtime posture as the services.
#[tokio::main]
async fn main() -> ExitCode {
    bootstrap_env_from_dotenv();

    // The CLI is the root span of every `bss <cmd>` trace. Held for the whole
    // process so the guard flushes on exit.
    let _telemetry = bss_telemetry::init_telemetry("cli");

    let cli = Cli::parse();
    match cli.command {
        Some(Command::Catalog(args)) => commands::catalog::run(args).await,
        Some(Command::Clock(args)) => commands::clock::run(args),
        Some(Command::Som(args)) => commands::som::run(args).await,
        Some(Command::Usage(args)) => commands::usage::run(args).await,
        // `bss` with no subcommand → the REPL (canonical cockpit). Not yet ported;
        // a following slice lands it. Fail loudly rather than silently no-op.
        None => {
            eprintln!("the interactive REPL is not yet ported in this slice; try `bss clock now`");
            ExitCode::from(1)
        }
    }
}

/// Load `<repo>/.env` into the process environment if a key isn't already set.
///
/// The cockpit REPL needs `BSS_DB_URL` + `BSS_OPERATOR_COCKPIT_API_TOKEN` as
/// process env (read directly, not via a settings struct), and `bss` isn't run
/// under a sourced shell. Existing env always wins — an exported value from the
/// parent shell or compose file is never overwritten. Port of Python's
/// `_bootstrap_env_from_dotenv`.
fn bootstrap_env_from_dotenv() {
    let Some(env_path) = repo_dotenv_path() else {
        return;
    };
    let Ok(contents) = std::fs::read_to_string(&env_path) else {
        return;
    };
    for raw in contents.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        // Strip optional surrounding quotes — common in .env files.
        let value = value.trim().trim_matches('"').trim_matches('\'');
        if !key.is_empty() && std::env::var_os(key).is_none() {
            // Safety: single-threaded startup, before any thread that reads env.
            std::env::set_var(key, value);
        }
    }
}

/// `<repo>/.env` — Python resolves `parents[2]` from the module; here the binary
/// can't know its source path at runtime, so we walk up from the current dir
/// looking for a `.env` beside a `rust/` sibling (the repo root).
fn repo_dotenv_path() -> Option<PathBuf> {
    let mut dir = std::env::current_dir().ok()?;
    loop {
        let candidate = dir.join(".env");
        if candidate.exists() && dir.join("rust").is_dir() {
            return Some(candidate);
        }
        if !dir.pop() {
            return None;
        }
    }
}
