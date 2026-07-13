//! Tool-description parity vs the Python oracle. Pure — runs in CI.
//!
//! The LLM-facing description (Python's stripped docstring) is a behavioural
//! contract with the model (R2). `golden/tool_descriptions.json` captures the
//! full `{name: description}` map from the Python registry; each tool family
//! validates its slice as it lands. Slice 1 pins the `clock.*` family.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use bss_orchestrator::default_registry;
use serde_json::Value;

#[test]
fn clock_tool_descriptions_match_python_oracle() {
    let golden: Value = serde_json::from_str(include_str!("golden/tool_descriptions.json"))
        .expect("parse tool-description golden");
    let registry = default_registry();

    for name in [
        "clock.now",
        "clock.advance",
        "clock.freeze",
        "clock.unfreeze",
    ] {
        let tool = registry
            .get(name)
            .unwrap_or_else(|| panic!("{name} registered"));
        let expected = golden[name]
            .as_str()
            .unwrap_or_else(|| panic!("golden has {name}"));
        assert_eq!(tool.description, expected, "description drift for {name}");
    }
}
