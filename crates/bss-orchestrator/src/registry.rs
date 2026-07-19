//! The full operator tool registry — the single source of truth for the LLM tool
//! surface, consumed by the cockpit portal, `bss ask`, the REPL, and the scenario
//! runner. Port of the registration block in Python's `graph.build_tools`.
//!
//! Each caller builds the typed service clients with its own auth (the CLI over the
//! default `BSS_API_TOKEN`; the CSR portal over its named `operator_cockpit` token)
//! and hands them here. The observability (`trace.*`) and knowledge (`knowledge.*`)
//! families register only when their infra handles are supplied — a caller without a
//! Jaeger endpoint or a knowledge pool simply omits them, mirroring Python (where
//! `knowledge.*` is gated on `BSS_KNOWLEDGE_ENABLED` and trace needs Jaeger).

use bss_clients::{
    AuditClient, CatalogClient, ComClient, CrmClient, InventoryClient, JaegerClient,
    MediationClient, PaymentClient, ProvisioningClient, SomClient, SubscriptionClient,
};
use sqlx::PgPool;

use crate::tools::{self, ToolRegistry};

/// The typed per-service clients the tool families wrap. One field per BSS service;
/// `inventory` shares CRM's base URL (inventory lives inside the CRM service).
pub struct RegistryClients {
    pub catalog: CatalogClient,
    pub crm: CrmClient,
    pub inventory: InventoryClient,
    pub payment: PaymentClient,
    pub com: ComClient,
    pub som: SomClient,
    pub subscription: SubscriptionClient,
    pub mediation: MediationClient,
    pub provisioning: ProvisioningClient,
}

/// Infra handles for the observability + knowledge families. Each optional: absent
/// handles omit that family. `trace.*` is all-or-nothing (it needs the Jaeger client
/// and both audit surfaces); `knowledge.*` needs only the pool.
#[derive(Default)]
pub struct RegistryExtras {
    /// Jaeger query client for `trace.get`.
    pub jaeger: Option<JaegerClient>,
    /// Audit surface on COM — resolves `trace.for_order`.
    pub audit_com: Option<AuditClient>,
    /// Audit surface on subscription — resolves `trace.for_subscription`.
    pub audit_sub: Option<AuditClient>,
    /// FTS pool for `knowledge.search` / `knowledge.get` (only when
    /// `BSS_KNOWLEDGE_ENABLED` and a `BSS_DB_URL` pool is reachable).
    pub knowledge_pool: Option<PgPool>,
}

/// Build the full operator surface: every read + write family, plus `trace.*` and
/// `knowledge.*` when [`RegistryExtras`] supplies their handles. The
/// `operator_cockpit` profile is a coverage view over this set; `bss ask` / the REPL
/// use the whole thing (`tool_filter = None`).
pub fn build_registry(clients: &RegistryClients, extras: RegistryExtras) -> ToolRegistry {
    let mut r = ToolRegistry::new();

    // ── Reads ────────────────────────────────────────────────────────────────
    tools::clock::register_clock_tools(&mut r);
    tools::catalog::register_catalog_tools(&mut r, clients.catalog.clone());
    tools::customer::register_customer_tools(
        &mut r,
        clients.crm.clone(),
        clients.subscription.clone(),
    );
    tools::case::register_case_tools(&mut r, clients.crm.clone());
    tools::ticket::register_ticket_tools(&mut r, clients.crm.clone());
    tools::port_request::register_port_request_tools(&mut r, clients.crm.clone());
    tools::ops::register_ops_tools(&mut r, clients.crm.clone());
    tools::subscription::register_subscription_tools(&mut r, clients.subscription.clone());
    tools::payment::register_payment_tools(&mut r, clients.payment.clone());
    tools::order::register_order_tools(&mut r, clients.com.clone());
    tools::som::register_som_tools(&mut r, clients.som.clone());
    tools::inventory::register_inventory_tools(&mut r, clients.inventory.clone());
    tools::provisioning::register_provisioning_tools(&mut r, clients.provisioning.clone());
    tools::promo::register_promo_tools(&mut r, clients.catalog.clone());
    tools::usage::register_usage_tools(&mut r, clients.mediation.clone());

    // ── Writes ───────────────────────────────────────────────────────────────
    // The operator surface carries the full write set; destructive verbs are gated
    // by the loop's propose-then-confirm wrapper, not by omission from the registry.
    tools::customer::register_customer_write_tools(&mut r, clients.crm.clone());
    tools::case::register_case_write_tools(&mut r, clients.crm.clone());
    tools::ticket::register_ticket_write_tools(&mut r, clients.crm.clone());
    tools::port_request::register_port_request_write_tools(&mut r, clients.crm.clone());
    tools::subscription::register_subscription_write_tools(&mut r, clients.subscription.clone());
    tools::payment::register_payment_write_tools(&mut r, clients.payment.clone());
    tools::order::register_order_write_tools(&mut r, clients.com.clone());
    tools::inventory::register_inventory_write_tools(&mut r, clients.inventory.clone());
    tools::provisioning::register_provisioning_write_tools(&mut r, clients.provisioning.clone());
    tools::promo::register_promo_write_tools(&mut r, clients.catalog.clone());
    tools::catalog::register_catalog_admin_write_tools(&mut r, clients.catalog.clone());
    // `usage.simulate` — the LLM-hidden mediation write. Not on the model's surface,
    // but scenarios drive it directly (deterministic usage burn), so it must be in the
    // registry. (Its sibling `register_usage_tools` above covers the reads.)
    tools::usage::register_usage_write_tools(&mut r, clients.mediation.clone());

    // ── Observability (trace.*) — needs Jaeger + both audit surfaces ─────────
    if let (Some(jaeger), Some(audit_com), Some(audit_sub)) =
        (extras.jaeger, extras.audit_com, extras.audit_sub)
    {
        tools::trace::register_trace_tools(&mut r, jaeger, audit_com, audit_sub);
    }

    // ── Knowledge (knowledge.*) — operator_cockpit only, needs the FTS pool ──
    if let Some(pool) = extras.knowledge_pool {
        tools::knowledge::register_knowledge_tools(&mut r, pool);
    }

    r
}
