//! `bss` command groups. Each module is one Typer sub-app in the Python CLI.
//!
//! The thin-command pattern (set by [`clock`]): a `clap::Args` struct with a
//! nested subcommand enum, a `run(args) -> ExitCode` entry, and — for the
//! client-backed groups that follow — a single `bss-clients` call per leaf. No
//! business logic in a command; the CLI calls the orchestrator or `bss-clients`
//! and nothing more (CLAUDE.md).

pub mod catalog;
pub mod clock;
pub mod order;
pub mod som;
pub mod subscription;
pub mod usage;

use serde_json::Value;

/// Indented JSON for the `*-show` debug commands. Python `rprint`s a rich
/// pretty-repr of the dict; we emit indented JSON — the exact glyphs differ but the
/// content is identical (these are human debug dumps, not golden renderers).
pub(crate) fn pretty(v: &Value) -> String {
    serde_json::to_string_pretty(v).unwrap_or_else(|_| v.to_string())
}
