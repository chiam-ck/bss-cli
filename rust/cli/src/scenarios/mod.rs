//! YAML scenario runner — port of `cli/bss_cli/scenarios/`.
//!
//! A scenario is a YAML file validated against [`schema::Scenario`]. The runner walks
//! `setup → steps → teardown`, executing each step in its declared mode (`action:` →
//! deterministic tool call, `ask:` → LLM). Captures flow through the `ScenarioContext`
//! so later steps can reference `{{ variables }}`.
//!
//! **This slice:** the schema + [`load_scenario`], plus the deterministic runner
//! (`action:` / `assert:` steps, setup reset/freeze, teardown unfreeze, captures) and
//! the report. `ask:` / `http:` / `file:` executors land in the following slices.

pub mod actions;
pub mod assertions;
pub mod context;
pub mod file_step;
pub mod http_step;
pub mod llm_executor;
pub mod reporting;
pub mod runner;
pub mod schema;

use std::path::Path;

pub use runner::{run_scenario, ScenarioResult};
pub use schema::Scenario;

/// Parse + validate a YAML scenario file. Port of `runner.load_scenario`: the
/// top-level YAML must be a mapping, unknown fields are rejected (`deny_unknown_fields`),
/// and the two post-parse invariants ([`Scenario::validate`]) run before returning.
/// Errors are returned as display strings (the CLI renders them; there's no need for a
/// typed error in this surface).
pub fn load_scenario(path: &Path) -> Result<Scenario, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    // Distinguish "not a mapping" from a field error, matching Python's ValueError.
    let doc: serde_yaml::Value =
        serde_yaml::from_str(&text).map_err(|e| format!("{}: {e}", path.display()))?;
    if !doc.is_mapping() {
        return Err(format!(
            "{}: top-level YAML must be a mapping",
            path.display()
        ));
    }
    let scenario: Scenario =
        serde_yaml::from_value(doc).map_err(|e| format!("{}: {e}", path.display()))?;
    scenario
        .validate()
        .map_err(|e| format!("{}: {e}", path.display()))?;
    Ok(scenario)
}
