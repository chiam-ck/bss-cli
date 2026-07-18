//! `bss admin ...` — operator-only tools. Port of `cli/bss_cli/commands/admin.py`.
//!
//! The Python `admin` app mounts three things: `catalog` (offering/price management,
//! ported here), `knowledge` (FTS reindex/search — deferred, needs the `bss-knowledge`
//! crate + a DB session), and `reset` (the cross-service operational-data wipe — needs
//! a ported `AdminClient` + per-target fan-out). This slice lands `catalog`; the other
//! two follow with their own client/DB plumbing.

use std::process::ExitCode;

use clap::{Args, Subcommand};

#[derive(Args)]
pub struct AdminArgs {
    #[command(subcommand)]
    command: AdminCommand,
}

#[derive(Subcommand)]
enum AdminCommand {
    /// Operator catalog management (offerings, prices, windows, migrations).
    Catalog(super::admin_catalog::AdminCatalogArgs),
}

pub async fn run(args: AdminArgs) -> ExitCode {
    match args.command {
        AdminCommand::Catalog(a) => super::admin_catalog::run(a).await,
    }
}
