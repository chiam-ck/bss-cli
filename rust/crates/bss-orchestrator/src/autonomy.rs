//! v1.5 — operator autonomy mode for the cockpit's multi-step LLM loop. Port of
//! `orchestrator/bss_orchestrator/autonomy.py`.
//!
//! One env var, two valid values, **fail-closed at process boot**.
//!
//! `BSS_REPL_LLM_AUTONOMY` (default `granular`):
//!
//! - `granular` — every destructive step in a compound action gates on its own
//!   `/confirm`. Maximum operator control. Behaviour-preserving for every call
//!   site that existed pre-v1.5.
//! - `batched` — the FIRST destructive step in a `/confirm`-resumed loop gates;
//!   subsequent destructive steps in the same loop execute autonomously. The
//!   operator authorises the plan once and the loop runs to completion.
//!
//! Unknown values raise [`AutonomyMisconfigured`] at startup — the same
//! fail-closed shape as `BSS_API_TOKEN=changeme`. Silent default-on-typo is the
//! kind of quiet contract drift the v0.9 named-token work explicitly refused; this
//! module honours the same line.
//!
//! The destructive-tool list ([`crate::safety::DESTRUCTIVE_TOOLS`]) is unchanged.
//! The autonomy mode controls *how many* `/confirm`s a compound action needs, NOT
//! *which* tools require one.
//!
//! **Doctrine (CLAUDE.md):** don't read `BSS_REPL_LLM_AUTONOMY` outside this
//! module — [`read_autonomy_mode`] is the single seam. The mode is process-scoped
//! (read once at startup, cached on the portal's state / the REPL's module-level
//! `_AUTONOMY_MODE`).

use crate::safety::AutonomyMode;

pub const VALID_AUTONOMY_MODES: &[&str] = &["granular", "batched"];
pub const DEFAULT_AUTONOMY_MODE: &str = "granular";

const ENV_VAR: &str = "BSS_REPL_LLM_AUTONOMY";

/// Returned at boot when `BSS_REPL_LLM_AUTONOMY` is set to an unrecognised value.
/// Fail-closed: the process refuses to start rather than silently defaulting to
/// `granular`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutonomyMisconfigured {
    pub message: String,
}

impl std::fmt::Display for AutonomyMisconfigured {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for AutonomyMisconfigured {}

/// Read `BSS_REPL_LLM_AUTONOMY` from the environment.
///
/// Unset or empty returns [`DEFAULT_AUTONOMY_MODE`]. Any other value is an error.
/// Whitespace is stripped and case normalised before validation, so
/// `BSS_REPL_LLM_AUTONOMY=Batched` and `="  granular  "` both load cleanly.
pub fn read_autonomy_mode() -> Result<AutonomyMode, AutonomyMisconfigured> {
    let raw = std::env::var(ENV_VAR).unwrap_or_default();
    let raw = raw.trim().to_lowercase();
    if raw.is_empty() {
        return Ok(mode_of(DEFAULT_AUTONOMY_MODE));
    }
    if !VALID_AUTONOMY_MODES.contains(&raw.as_str()) {
        // Python renders the value with `!r` (single quotes) and the valid set with
        // `sorted()` — ['batched', 'granular'].
        let mut valid: Vec<&str> = VALID_AUTONOMY_MODES.to_vec();
        valid.sort_unstable();
        let valid = valid
            .iter()
            .map(|v| format!("'{v}'"))
            .collect::<Vec<_>>()
            .join(", ");
        return Err(AutonomyMisconfigured {
            message: format!(
                "{ENV_VAR}='{raw}' is not valid. Set to one of [{valid}] or leave \
                 unset to default to '{DEFAULT_AUTONOMY_MODE}'."
            ),
        });
    }
    Ok(mode_of(&raw))
}

fn mode_of(name: &str) -> AutonomyMode {
    match name {
        "batched" => AutonomyMode::Batched,
        _ => AutonomyMode::Granular,
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    /// One test — the env var is process-global, so the cases run serially.
    #[test]
    fn read_autonomy_mode_cases() {
        // Unset → the default, NOT an error.
        std::env::remove_var(ENV_VAR);
        assert_eq!(read_autonomy_mode(), Ok(AutonomyMode::Granular));

        // Empty → the default too (Python's `if not raw`).
        std::env::set_var(ENV_VAR, "");
        assert_eq!(read_autonomy_mode(), Ok(AutonomyMode::Granular));

        std::env::set_var(ENV_VAR, "granular");
        assert_eq!(read_autonomy_mode(), Ok(AutonomyMode::Granular));
        std::env::set_var(ENV_VAR, "batched");
        assert_eq!(read_autonomy_mode(), Ok(AutonomyMode::Batched));

        // Whitespace + case are normalised.
        std::env::set_var(ENV_VAR, "  Batched  ");
        assert_eq!(read_autonomy_mode(), Ok(AutonomyMode::Batched));
        std::env::set_var(ENV_VAR, "GRANULAR");
        assert_eq!(read_autonomy_mode(), Ok(AutonomyMode::Granular));

        // Fail-closed on a typo — never silently default.
        std::env::set_var(ENV_VAR, "granlar");
        let err = read_autonomy_mode().expect_err("a typo must fail closed");
        assert_eq!(
            err.message,
            "BSS_REPL_LLM_AUTONOMY='granlar' is not valid. Set to one of \
             ['batched', 'granular'] or leave unset to default to 'granular'."
        );

        // "on"/"true"-style values are not a sneaky alias for batched.
        std::env::set_var(ENV_VAR, "true");
        assert!(read_autonomy_mode().is_err());

        std::env::remove_var(ENV_VAR);
    }
}
