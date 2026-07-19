//! `bss clock ...` — time helpers. Port of `cli/bss_cli/commands/clock.py`.
//!
//! v0.1 uses wall-clock time (a proper scenario clock service doesn't exist yet);
//! `advance` is a later-phase feature that prints a not-implemented notice and
//! exits 2 — reproduced faithfully.

use std::process::ExitCode;

use chrono::SubsecRound;
use clap::{Args, Subcommand};

#[derive(Args)]
pub struct ClockArgs {
    #[command(subcommand)]
    command: ClockCommand,
}

#[derive(Subcommand)]
enum ClockCommand {
    /// Print the current time in ISO-8601 UTC.
    Now,
    /// (Later phase) Advance the scenario clock. v0.1 prints a not-implemented notice.
    Advance {
        /// e.g. 30d, 1h, 15m
        duration: String,
    },
}

pub fn run(args: ClockArgs) -> ExitCode {
    match args.command {
        ClockCommand::Now => {
            println!("{}", now_iso());
            ExitCode::SUCCESS
        }
        ClockCommand::Advance { duration } => {
            // Python prints a yellow notice + the requested delta, then exits 2.
            eprintln!(
                "clock.advance is a later-phase feature — scenario clock service \
                 not wired in v0.1."
            );
            eprintln!("requested delta: {duration}");
            ExitCode::from(2)
        }
    }
}

/// Current UTC truncated to whole seconds, formatted as Python's
/// `datetime.now(timezone.utc).replace(microsecond=0).isoformat()` — i.e.
/// `YYYY-MM-DDTHH:MM:SS+00:00`.
fn now_iso() -> String {
    bss_clock::now()
        .trunc_subsecs(0)
        .format("%Y-%m-%dT%H:%M:%S%:z")
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn now_iso_is_second_precision_utc_isoformat() {
        let s = now_iso();
        // Shape: 2026-07-18T12:34:56+00:00 — no subsecond, explicit +00:00 offset.
        assert!(s.ends_with("+00:00"), "{s}");
        assert_eq!(s.len(), "2026-07-18T12:34:56+00:00".len(), "{s}");
        assert!(!s.contains('.'), "no subseconds: {s}");
        // The 'T' separator sits between date and time.
        assert_eq!(s.as_bytes()[10], b'T', "{s}");
    }
}
