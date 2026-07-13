//! Tool-description + profile-wiring parity vs the Python oracle. Pure — CI.
//!
//! The LLM-facing description (Python's stripped docstring) is a behavioural
//! contract with the model (R2). `golden/tool_descriptions.json` captures the
//! full `{name: description}` map from the Python registry; each tool family
//! validates its slice as it lands. Slice 1 pinned `clock.*`; slice 2 adds the
//! catalog read family + profile membership.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use bss_clients::{
    AuditClient, CatalogClient, ComClient, CrmClient, InventoryClient, JaegerClient,
    MediationClient, PaymentClient, ProvisioningClient, SomClient, SubscriptionClient,
    TokenAuthProvider,
};
use bss_orchestrator::tools::{CUSTOMER_SELF_SERVE, OPERATOR_COCKPIT};
use bss_orchestrator::{default_registry, ToolRegistry};
use serde_json::Value;

fn golden() -> Value {
    serde_json::from_str(include_str!("golden/tool_descriptions.json"))
        .expect("parse tool-description golden")
}

/// A registry with every ported read family registered (the clients point nowhere —
/// registration makes no network call).
fn registry_with_catalog() -> ToolRegistry {
    let mut reg = default_registry();
    let auth = Arc::new(TokenAuthProvider::new("x").unwrap());
    let catalog = CatalogClient::new("http://localhost:8001", auth.clone()).unwrap();
    bss_orchestrator::tools::catalog::register_catalog_tools(&mut reg, catalog);
    let crm = CrmClient::new("http://localhost:8002", auth.clone()).unwrap();
    let subscription = SubscriptionClient::new("http://localhost:8006", auth.clone()).unwrap();
    bss_orchestrator::tools::customer::register_customer_tools(
        &mut reg,
        crm.clone(),
        subscription.clone(),
    );
    bss_orchestrator::tools::customer::register_customer_write_tools(&mut reg, crm.clone());
    bss_orchestrator::tools::subscription::register_subscription_tools(
        &mut reg,
        subscription.clone(),
    );
    bss_orchestrator::tools::subscription::register_subscription_write_tools(
        &mut reg,
        subscription,
    );
    let payment = PaymentClient::new("http://localhost:8003", auth.clone()).unwrap();
    bss_orchestrator::tools::payment::register_payment_tools(&mut reg, payment.clone());
    bss_orchestrator::tools::payment::register_payment_write_tools(&mut reg, payment);
    let com = ComClient::new("http://localhost:8004", auth.clone()).unwrap();
    bss_orchestrator::tools::order::register_order_tools(&mut reg, com.clone());
    bss_orchestrator::tools::order::register_order_write_tools(&mut reg, com);
    let som = SomClient::new("http://localhost:8005", auth.clone()).unwrap();
    bss_orchestrator::tools::som::register_som_tools(&mut reg, som);
    let inventory = InventoryClient::new("http://localhost:8002", auth.clone()).unwrap();
    bss_orchestrator::tools::inventory::register_inventory_tools(&mut reg, inventory);
    let provisioning = ProvisioningClient::new("http://localhost:8010", auth.clone()).unwrap();
    bss_orchestrator::tools::provisioning::register_provisioning_tools(&mut reg, provisioning);
    let mediation = MediationClient::new("http://localhost:8007", auth.clone()).unwrap();
    bss_orchestrator::tools::usage::register_usage_tools(&mut reg, mediation);
    // A second CatalogClient handle backs promo.show.
    let catalog2 = CatalogClient::new("http://localhost:8001", auth).unwrap();
    bss_orchestrator::tools::promo::register_promo_tools(&mut reg, catalog2);
    bss_orchestrator::tools::ticket::register_ticket_tools(&mut reg, crm.clone());
    bss_orchestrator::tools::ticket::register_ticket_write_tools(&mut reg, crm.clone());
    bss_orchestrator::tools::case::register_case_tools(&mut reg, crm.clone());
    bss_orchestrator::tools::case::register_case_write_tools(&mut reg, crm.clone());
    bss_orchestrator::tools::port_request::register_port_request_tools(&mut reg, crm.clone());
    bss_orchestrator::tools::ops::register_ops_tools(&mut reg, crm);
    // trace — Jaeger (no auth) + two audit clients (com/subscription base URLs).
    let jaeger = JaegerClient::new("http://localhost:16686").unwrap();
    let audit_auth = Arc::new(TokenAuthProvider::new("x").unwrap());
    let audit_com = AuditClient::new("http://localhost:8004", audit_auth.clone()).unwrap();
    let audit_sub = AuditClient::new("http://localhost:8006", audit_auth).unwrap();
    bss_orchestrator::tools::trace::register_trace_tools(&mut reg, jaeger, audit_com, audit_sub);
    // knowledge — a lazy pool (no connection is made at registration).
    let pool = sqlx::PgPool::connect_lazy("postgres://x:x@localhost/x").unwrap();
    bss_orchestrator::tools::knowledge::register_knowledge_tools(&mut reg, pool);
    reg
}

// Uses `registry_with_catalog`, which builds a lazy `PgPool` for the knowledge
// tools — pool construction needs a tokio runtime (no connection is made).
#[tokio::test]
async fn tool_descriptions_match_python_oracle() {
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
        "subscription.get",
        "subscription.list_for_customer",
        "subscription.get_balance",
        "subscription.get_esim_activation",
        "payment.list_methods",
        "payment.get_attempt",
        "payment.list_attempts",
        "order.get",
        "order.list",
        "order.wait_until",
        "service_order.get",
        "service_order.list_for_order",
        "service.get",
        "service.list_for_subscription",
        "inventory.msisdn.list_available",
        "inventory.msisdn.get",
        "inventory.msisdn.count",
        "inventory.esim.list_available",
        "inventory.esim.get_activation",
        "provisioning.get_task",
        "provisioning.list_tasks",
        "usage.history",
        "events.list",
        "agents.list",
        "ticket.get",
        "ticket.list",
        "case.get",
        "case.list",
        "case.show_transcript_for",
        "promo.show",
        "port_request.list",
        "port_request.get",
        "trace.get",
        "trace.for_order",
        "trace.for_subscription",
        "knowledge.search",
        "knowledge.get",
        "customer.create",
        "customer.update_contact",
        "customer.add_contact_medium",
        "customer.remove_contact_medium",
        "customer.attest_kyc",
        "customer.close",
        "interaction.log",
        "case.open",
        "case.close",
        "case.add_note",
        "case.transition",
        "case.update_priority",
        "ticket.open",
        "ticket.assign",
        "ticket.transition",
        "ticket.resolve",
        "ticket.close",
        "ticket.cancel",
        "subscription.terminate",
        "subscription.purchase_vas",
        "subscription.renew_now",
        "subscription.tick_renewals_now",
        "subscription.schedule_plan_change",
        "subscription.cancel_pending_plan_change",
        "subscription.migrate_to_new_price",
        "order.create",
        "order.cancel",
        "payment.add_card",
        "payment.remove_method",
        "payment.charge",
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
fn subscription_canonical_reads_are_operator_only() {
    // The canonical subscription reads are operator_cockpit — the chat surface
    // sees `subscription.*_mine` ownership wrappers instead (a later slice).
    for name in [
        "subscription.get",
        "subscription.list_for_customer",
        "subscription.get_balance",
        "subscription.get_esim_activation",
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
fn order_payment_writes_profile_and_destructive() {
    use bss_orchestrator::DESTRUCTIVE_TOOLS;
    for name in [
        "order.create",
        "order.cancel",
        "payment.add_card",
        "payment.remove_method",
        "payment.charge",
    ] {
        assert!(
            OPERATOR_COCKPIT.contains(&name),
            "{name} missing from operator_cockpit"
        );
    }
    // order.cancel + payment.remove_method are destructive; create/add_card/charge not.
    for name in ["order.cancel", "payment.remove_method"] {
        assert!(
            DESTRUCTIVE_TOOLS.contains(&name),
            "{name} must be destructive"
        );
    }
    for name in ["order.create", "payment.add_card", "payment.charge"] {
        assert!(
            !DESTRUCTIVE_TOOLS.contains(&name),
            "{name} must NOT be destructive"
        );
    }
}

#[test]
fn subscription_writes_profile_destructive_and_hidden() {
    use bss_orchestrator::tools::LLM_HIDDEN_TOOLS;
    use bss_orchestrator::DESTRUCTIVE_TOOLS;
    for name in [
        "subscription.terminate",
        "subscription.purchase_vas",
        "subscription.renew_now",
        "subscription.tick_renewals_now",
        "subscription.schedule_plan_change",
        "subscription.cancel_pending_plan_change",
        "subscription.migrate_to_new_price",
    ] {
        assert!(
            OPERATOR_COCKPIT.contains(&name),
            "{name} missing from operator_cockpit"
        );
    }
    // terminate is destructive; purchase_vas explicitly is NOT (it adds allowance).
    assert!(DESTRUCTIVE_TOOLS.contains(&"subscription.terminate"));
    assert!(!DESTRUCTIVE_TOOLS.contains(&"subscription.purchase_vas"));
    // migrate_to_new_price is hidden from the LLM (operator/scenario only).
    assert!(LLM_HIDDEN_TOOLS.contains(&"subscription.migrate_to_new_price"));
}

#[test]
fn case_ticket_writes_are_operator_and_destructive_gated() {
    use bss_orchestrator::DESTRUCTIVE_TOOLS;
    for name in [
        "case.open",
        "case.close",
        "case.add_note",
        "case.transition",
        "case.update_priority",
        "ticket.open",
        "ticket.assign",
        "ticket.transition",
        "ticket.resolve",
        "ticket.close",
        "ticket.cancel",
    ] {
        assert!(
            OPERATOR_COCKPIT.contains(&name),
            "{name} missing from operator_cockpit"
        );
    }
    // case.close + ticket.cancel are destructive; the rest are not.
    for name in ["case.close", "ticket.cancel"] {
        assert!(
            DESTRUCTIVE_TOOLS.contains(&name),
            "{name} must be destructive"
        );
    }
    for name in [
        "case.open",
        "case.transition",
        "ticket.resolve",
        "ticket.close",
    ] {
        assert!(
            !DESTRUCTIVE_TOOLS.contains(&name),
            "{name} must NOT be destructive"
        );
    }
}

#[test]
fn customer_writes_are_operator_and_destructive_gated() {
    use bss_orchestrator::DESTRUCTIVE_TOOLS;
    for name in [
        "customer.create",
        "customer.update_contact",
        "customer.add_contact_medium",
        "customer.remove_contact_medium",
        "customer.attest_kyc",
        "customer.close",
        "interaction.log",
    ] {
        assert!(
            OPERATOR_COCKPIT.contains(&name),
            "{name} missing from operator_cockpit"
        );
    }
    // The two account-mutating writes are destructive (safety-gated).
    for name in ["customer.remove_contact_medium", "customer.close"] {
        assert!(
            DESTRUCTIVE_TOOLS.contains(&name),
            "{name} must be in DESTRUCTIVE_TOOLS"
        );
    }
    // ...the others are not.
    for name in [
        "customer.create",
        "customer.update_contact",
        "interaction.log",
    ] {
        assert!(
            !DESTRUCTIVE_TOOLS.contains(&name),
            "{name} must NOT be destructive"
        );
    }
}

#[test]
fn trace_and_knowledge_are_operator_only() {
    // trace.* is observability; knowledge.* is operator_cockpit-only by doctrine
    // guard 15 — never customer_self_serve.
    for name in [
        "trace.get",
        "trace.for_order",
        "trace.for_subscription",
        "knowledge.search",
        "knowledge.get",
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
fn crm_catalog_read_batch_is_in_operator_profile() {
    // ticket / case / promo / port_request reads are operator_cockpit tools (the
    // chat surface sees only `case.list_for_me` / `case.open_for_me`).
    for name in [
        "ticket.get",
        "ticket.list",
        "case.get",
        "case.list",
        "case.show_transcript_for",
        "promo.show",
        "port_request.list",
        "port_request.get",
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
fn operator_read_batch_is_in_operator_profile() {
    // order / SOM / inventory / provisioning / usage / events / agents reads are all
    // operator_cockpit-surface tools (never customer_self_serve).
    for name in [
        "order.get",
        "order.list",
        "order.wait_until",
        "service_order.get",
        "service_order.list_for_order",
        "service.get",
        "service.list_for_subscription",
        "inventory.msisdn.list_available",
        "inventory.msisdn.get",
        "inventory.msisdn.count",
        "inventory.esim.list_available",
        "inventory.esim.get_activation",
        "provisioning.get_task",
        "provisioning.list_tasks",
        "usage.history",
        "events.list",
        "agents.list",
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
fn payment_canonical_reads_are_operator_only() {
    // Canonical payment reads are operator_cockpit — the chat surface sees the
    // ownership-bound `payment.*_mine` wrappers (a later slice) instead.
    for name in [
        "payment.list_methods",
        "payment.get_attempt",
        "payment.list_attempts",
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

#[tokio::test]
async fn surface_intersects_profile_with_registry() {
    // The operator_cockpit surface is the intersection of the profile with what's
    // registered — never the full 90-tool profile.
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
    // A profile tool that isn't registered yet must not appear (promo/inventory/
    // provisioning writes are a later slice).
    assert!(!surface.contains(&"promo.create".to_string()));
}
