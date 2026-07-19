//! `bss` command groups. Each module is one Typer sub-app in the Python CLI.
//!
//! The thin-command pattern (set by [`clock`]): a `clap::Args` struct with a
//! nested subcommand enum, a `run(args) -> ExitCode` entry, and — for the
//! client-backed groups that follow — a single `bss-clients` call per leaf. No
//! business logic in a command; the CLI calls the orchestrator or `bss-clients`
//! and nothing more (CLAUDE.md).

pub mod admin;
pub mod admin_catalog;
pub mod admin_knowledge;
pub mod admin_migrate;
pub mod admin_seed;
pub mod ask;
pub mod branding;
pub mod case;
pub mod catalog;
pub mod clock;
pub mod customer;
pub mod external_calls;
pub mod inventory;
pub mod onboard;
pub mod order;
pub mod payment;
pub mod promo;
pub mod prov;
pub mod scenario;
pub mod som;
pub mod subscription;
pub mod ticket;
pub mod trace;
pub mod usage;

use rust_decimal::Decimal;
use serde_json::Value;
use std::str::FromStr;

/// Indented JSON for the `*-show` debug commands. Python `rprint`s a rich
/// pretty-repr of the dict; we emit indented JSON — the exact glyphs differ but the
/// content is identical (these are human debug dumps, not golden renderers).
pub(crate) fn pretty(v: &Value) -> String {
    serde_json::to_string_pretty(v).unwrap_or_else(|_| v.to_string())
}

/// `str(Decimal(s))` — validate + canonicalise a decimal string. `rust_decimal`'s
/// round-trip preserves scale (no trailing-zero stripping) the same way CPython's
/// `Decimal.__str__` does for plain decimals; leading whitespace/zeros are absorbed.
/// `None` ⇒ Python's `InvalidOperation`. Shared by `promo` and `admin catalog`, both
/// of which normalise a `--value`/`--price` before any async work.
pub(crate) fn normalize_decimal(s: &str) -> Option<String> {
    Decimal::from_str(s.trim()).ok().map(|d| d.to_string())
}

/// `datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()` — validate an
/// ISO-8601 moment and re-emit the canonical string the Python client would send.
/// Returns the isoformat string, or `Err(())` (the caller owns the
/// `Invalid ISO-8601 datetime: '<value>'` message + exit 2). Mirrors
/// `admin_catalog._parse_iso`.
pub(crate) fn parse_iso(value: &str) -> Result<String, ()> {
    use chrono::{DateTime, NaiveDate, NaiveDateTime};
    let s = value.replace('Z', "+00:00");
    // Offset-aware — re-emit with `+00:00`-style offset and always seconds.
    if let Ok(dt) = DateTime::parse_from_rfc3339(&s) {
        return Ok(dt.format("%Y-%m-%dT%H:%M:%S%:z").to_string());
    }
    for fmt in ["%Y-%m-%dT%H:%M:%S%:z", "%Y-%m-%dT%H:%M%:z"] {
        if let Ok(dt) = DateTime::parse_from_str(&s, fmt) {
            return Ok(dt.format("%Y-%m-%dT%H:%M:%S%:z").to_string());
        }
    }
    // Naive datetime.
    for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"] {
        if let Ok(dt) = NaiveDateTime::parse_from_str(&s, fmt) {
            return Ok(dt.format("%Y-%m-%dT%H:%M:%S").to_string());
        }
    }
    // Date-only → midnight, matching `fromisoformat("2026-06-10")`.
    if let Ok(d) = NaiveDate::parse_from_str(&s, "%Y-%m-%d") {
        if let Some(dt) = d.and_hms_opt(0, 0, 0) {
            return Ok(dt.format("%Y-%m-%dT%H:%M:%S").to_string());
        }
    }
    Err(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    #[test]
    fn normalize_decimal_preserves_scale() {
        assert_eq!(normalize_decimal("10").as_deref(), Some("10"));
        assert_eq!(normalize_decimal("10.50").as_deref(), Some("10.50"));
        assert_eq!(normalize_decimal(" 010.5 ").as_deref(), Some("10.5"));
        assert_eq!(normalize_decimal("nope"), None);
    }

    #[test]
    fn parse_iso_matches_python_isoformat() {
        assert_eq!(
            parse_iso("2026-01-01T00:00:00").unwrap(),
            "2026-01-01T00:00:00"
        );
        // `Z` → `+00:00`, seconds always present.
        assert_eq!(
            parse_iso("2026-01-01T00:00:00Z").unwrap(),
            "2026-01-01T00:00:00+00:00"
        );
        assert_eq!(
            parse_iso("2026-06-10T08:30").unwrap(),
            "2026-06-10T08:30:00"
        );
        assert_eq!(parse_iso("2026-06-10").unwrap(), "2026-06-10T00:00:00");
        assert!(parse_iso("bad").is_err());
    }
}
