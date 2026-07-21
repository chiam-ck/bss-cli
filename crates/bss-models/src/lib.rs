//! bss-models — shared model types + the release version.
//!
//! Rust port of `packages/bss-models`. The ~60 per-table `FromRow` structs are
//! ported **with each service** (P1–P4) against that service's golden contract
//! tests — that's where the dict-shape hazards (risk R1) are best caught — so
//! this crate starts with only the cross-cutting essentials. It grows table
//! modules as services land.
#![forbid(unsafe_code)]

/// Single source-of-truth release version (doctrine guard #14). Tracked the
/// Python `bss_models.BSS_RELEASE` during the migration so version strings stayed
/// identical at cutover; now independent — the Python oracle was retired at the
/// `v2.0.0` release (2026-07-21). See docs/PYTHON-ORACLE.md.
pub const BSS_RELEASE: &str = "2.0.0";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn release_is_current() {
        assert_eq!(BSS_RELEASE, "2.0.0");
    }
}
