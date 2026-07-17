//! `bss` command groups. Each module is one Typer sub-app in the Python CLI.
//!
//! The thin-command pattern (set by [`clock`]): a `clap::Args` struct with a
//! nested subcommand enum, a `run(args) -> ExitCode` entry, and — for the
//! client-backed groups that follow — a single `bss-clients` call per leaf. No
//! business logic in a command; the CLI calls the orchestrator or `bss-clients`
//! and nothing more (CLAUDE.md).

pub mod catalog;
pub mod clock;
