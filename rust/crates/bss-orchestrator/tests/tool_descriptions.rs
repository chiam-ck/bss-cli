//! Tool-description + profile-wiring parity vs the Python oracle. Pure — CI.
//!
//! The LLM-facing description (Python's stripped docstring) is a behavioural
//! contract with the model (R2). `golden/tool_descriptions.json` captures the
//! full `{name: description}` map from the Python registry; each tool family
//! validates its slice as it lands. Slice 1 pinned `clock.*`; slice 2 adds the
//! catalog read family + profile membership.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CatalogClient, TokenAuthProvider};
use bss_orchestrator::tools::{CUSTOMER_SELF_SERVE, OPERATOR_COCKPIT};
use bss_orchestrator::{default_registry, ToolRegistry};
use serde_json::Value;

fn golden() -> Value {
    serde_json::from_str(include_str!("golden/tool_descriptions.json"))
        .expect("parse tool-description golden")
}

/// A registry with clock + catalog tools (the catalog client points nowhere —
/// registration makes no network call).
fn registry_with_catalog() -> ToolRegistry {
    let mut reg = default_registry();
    let auth = Arc::new(TokenAuthProvider::new("x").unwrap());
    let client = CatalogClient::new("http://localhost:8001", auth).unwrap();
    bss_orchestrator::tools::catalog::register_catalog_tools(&mut reg, client);
    reg
}

#[test]
fn tool_descriptions_match_python_oracle() {
    let golden = golden();
    let registry = registry_with_catalog();

    let names = [
        "clock.now",
        "clock.advance",
        "clock.freeze",
        "clock.unfreeze",
        "catalog.list_offerings",
        "catalog.get_offering",
        "catalog.list_vas",
        "catalog.get_vas",
        "catalog.list_active_offerings",
        "catalog.get_active_price",
    ];
    for name in names {
        let tool = registry
            .get(name)
            .unwrap_or_else(|| panic!("{name} registered"));
        let expected = golden[name]
            .as_str()
            .unwrap_or_else(|| panic!("golden has {name}"));
        assert_eq!(tool.description, expected, "description drift for {name}");
    }
}

#[test]
fn catalog_reads_are_in_the_expected_profiles() {
    // operator_cockpit sees the full read family.
    for name in [
        "catalog.list_offerings",
        "catalog.get_offering",
        "catalog.list_vas",
        "catalog.get_vas",
        "catalog.list_active_offerings",
        "catalog.get_active_price",
    ] {
        assert!(
            OPERATOR_COCKPIT.contains(&name),
            "{name} missing from operator_cockpit"
        );
    }
    // customer_self_serve sees only the public catalog reads.
    for name in [
        "catalog.list_vas",
        "catalog.list_active_offerings",
        "catalog.get_offering",
    ] {
        assert!(
            CUSTOMER_SELF_SERVE.contains(&name),
            "{name} missing from customer_self_serve"
        );
    }
    // ...but NOT the internal price/full-list reads.
    for name in ["catalog.get_active_price", "catalog.list_offerings"] {
        assert!(
            !CUSTOMER_SELF_SERVE.contains(&name),
            "{name} must not be in customer_self_serve"
        );
    }
}

#[test]
fn surface_intersects_profile_with_registry() {
    // With only clock + catalog registered, the operator_cockpit surface is the
    // intersection — never the full 90-tool profile.
    let registry = registry_with_catalog();
    let surface: Vec<String> = registry
        .surface(Some("operator_cockpit"))
        .into_iter()
        .map(|s| s.name)
        .collect();
    assert!(surface.contains(&"clock.now".to_string()));
    assert!(surface.contains(&"catalog.get_offering".to_string()));
    // clock.freeze/advance/unfreeze are operator_cockpit + registered.
    assert!(surface.contains(&"clock.freeze".to_string()));
    // A profile tool that isn't registered yet must not appear.
    assert!(!surface.contains(&"customer.get".to_string()));
}
