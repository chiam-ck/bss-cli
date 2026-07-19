//! bss-models — shared model types + the release version.
//!
//! Rust port of `packages/bss-models`. The ~60 per-table `FromRow` structs are
//! ported **with each service** (P1–P4) against that service's golden contract
//! tests — that's where the dict-shape hazards (risk R1) are best caught — so
//! this crate starts with only the cross-cutting essentials. It grows table
//! modules as services land.
#![forbid(unsafe_code)]

/// Single source-of-truth release version (doctrine guard #14). Tracks the
/// Python `bss_models.BSS_RELEASE` during the migration so version strings stay
/// identical at cutover; it becomes independent once the Python tree is retired.
pub const BSS_RELEASE: &str = "1.8.1";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn release_matches_python_oracle() {
        // Pinned to packages/bss-models/bss_models/__init__.py BSS_RELEASE.
        assert_eq!(BSS_RELEASE, "1.8.1");
    }
}
