//! Tool-description + profile-wiring parity vs the Python oracle. Pure — CI.
//!
//! The LLM-facing description (Python's stripped docstring) is a behavioural
//! contract with the model (R2). `golden/tool_descriptions.json` captures the
//! full `{name: description}` map from the Python registry; each tool family
//! validates its slice as it lands. Slice 1 pinned `clock.*`; slice 2 adds the
//! catalog read family + profile membership.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{CatalogClient, CrmClient, SubscriptionClient, TokenAuthProvider};
use bss_orchestrator::tools::{CUSTOMER_SELF_SERVE, OPERATOR_COCKPIT};
use bss_orchestrator::{default_registry, ToolRegistry};
use serde_json::Value;

fn golden() -> Value {
    serde_json::from_str(include_str!("golden/tool_descriptions.json"))
        .expect("parse tool-description golden")
}

/// A registry with clock + catalog + CRM-read tools (the clients point nowhere —
/// registration makes no network call).
fn registry_with_catalog() -> ToolRegistry {
    let mut reg = default_registry();
    let auth = Arc::new(TokenAuthProvider::new("x").unwrap());
    let catalog = CatalogClient::new("http://localhost:8001", auth.clone()).unwrap();
    bss_orchestrator::tools::catalog::register_catalog_tools(&mut reg, catalog);
    let crm = CrmClient::new("http://localhost:8006", auth.clone()).unwrap();
    let subscription = SubscriptionClient::new("http://localhost:8007", auth).unwrap();
    bss_orchestrator::tools::customer::register_customer_tools(&mut reg, crm, subscription);
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
        "customer.get",
        "customer.list",
        "customer.find_by_msisdn",
        "customer.find_by_email",
        "customer.get_kyc_status",
        "interaction.list",
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
fn crm_reads_are_operator_only() {
    // The canonical CRM read tools are operator_cockpit — the chat surface sees
    // only the ownership-bound `*.mine` wrappers, never these unscoped reads.
    for name in [
        "customer.get",
        "customer.list",
        "customer.find_by_msisdn",
        "customer.find_by_email",
        "customer.get_kyc_status",
        "interaction.list",
    ] {
        assert!(
            OPERATOR_COCKPIT.contains(&name),
            "{name} missing from operator_cockpit"
        );
        assert!(
            !CUSTOMER_SELF_SERVE.contains(&name),
            "{name} must not be exposed to customer_self_serve"
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
    assert!(surface.contains(&"customer.get".to_string()));
    // clock.freeze/advance/unfreeze are operator_cockpit + registered.
    assert!(surface.contains(&"clock.freeze".to_string()));
    // A profile tool that isn't registered yet must not appear (order.* is a
    // later slice).
    assert!(!surface.contains(&"order.get".to_string()));
}
