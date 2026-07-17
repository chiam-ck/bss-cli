//! bss-csr — the operator cockpit portal (service 9002). Rust port of
//! `portals/csr`.
//!
//! Per v0.13 the v0.5 CSR portal pattern is retired: the browser is a surface over
//! the Postgres-backed cockpit `Conversation` store, and the canonical surface is
//! the CLI REPL. **No login** — `actor` for cockpit turns comes from
//! `.bss-cli/settings.toml` via `bss_cockpit::config::current()`, and the cockpit
//! is single-operator-by-design behind a secure perimeter (CLAUDE.md anti-pattern,
//! DECISIONS 2026-05-01). Amended v1.6: the browser is no longer *only* a veneer —
//! it carries first-class CRM screens (DECISIONS 2026-06-10).
//!
//! **`BSSApiTokenMiddleware` is deliberately NOT on this portal's inbound HTTP.**
//! Outbound calls carry the cockpit's named token via [`clients`].
//!
//! **Done:** the app skeleton (config, branding-aware MiniJinja templating over
//! the existing Jinja templates, static mounts, `/health`), [`views`] (the shared
//! snake_case/camelCase-lenient payload helpers every screen reads through), the
//! ASCII renderers (in `bss-cockpit`), the cockpit chat thread + SSE + `/confirm`,
//! and the [`customers`] CRM screen.
//!
//! **Remaining:** the rest of the CRM screens (cases / orders / catalog /
//! subscriptions / search), settings + branding + handoff.
#![forbid(unsafe_code)]

pub mod bubble;
pub mod clients;
pub mod cockpit;
pub mod config;
pub mod customers;
pub mod guards;
pub mod inflight;
pub mod routes;
pub mod sessions;
pub mod templating;
pub mod tool_row;
pub mod turn;
pub mod views;

use std::sync::Arc;

use axum::routing::{get, post};
use axum::Router;
use bss_cockpit::ConversationStore;
use bss_orchestrator::{AutonomyMode, ToolRegistry};
use minijinja::Environment;
use tower_http::services::ServeDir;

use clients::CockpitClients;
use config::Settings;

/// Shared application state (cheap to clone — everything behind `Arc`).
#[derive(Clone)]
pub struct AppState {
    pub env: Arc<Environment<'static>>,
    pub settings: Arc<Settings>,
    /// `None` when the perimeter token isn't provisioned (e.g. template-only
    /// tests); CRM screens then degrade section-by-section rather than 500.
    pub clients: Option<Arc<CockpitClients>>,
    /// The cockpit `Conversation` store — shared with the REPL via the `cockpit`
    /// schema. `None` only in template-only tests; the binary refuses to boot
    /// without it.
    pub store: Option<Arc<ConversationStore>>,
    /// The `operator_cockpit` tool surface the agent may reach. `None` without a
    /// perimeter token; the chat route then errors the turn.
    pub chat_registry: Option<Arc<ToolRegistry>>,
    /// v1.5 — read once at boot (fail-closed); the loop's destructive gating
    /// consults it.
    pub autonomy_mode: AutonomyMode,
    /// v1.6.1 — running turns, so a reconnect observes rather than re-drives.
    pub inflight: inflight::Inflight,
}

/// Build the cockpit router.
pub fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(routes::health))
        // The cockpit. The ONLY orchestrator-mediated routes in this portal.
        .route("/", get(cockpit::index))
        .route("/cockpit/new", post(cockpit::new_session))
        .route("/cockpit/:session_id", get(cockpit::thread))
        .route("/cockpit/:session_id/turn", post(cockpit::post_turn))
        .route("/cockpit/:session_id/reset", post(cockpit::post_reset))
        .route("/cockpit/:session_id/confirm", post(cockpit::post_confirm))
        .route("/cockpit/:session_id/focus", post(cockpit::post_focus))
        .route("/cockpit/:session_id/events", get(cockpit::events))
        // ── CRM screens (v1.6). Direct policy-gated reads/writes — no
        // orchestrator hop. Destructive verbs are confirm-gated in the handler.
        .route("/customers", get(customers::customers_list))
        .route("/customers/:customer_id", get(customers::customer_detail))
        .route(
            "/customers/:customer_id/interaction",
            post(customers::log_interaction),
        )
        .route("/customers/:customer_id/name", post(customers::update_name))
        .route(
            "/customers/:customer_id/contact",
            post(customers::add_contact),
        )
        .route(
            "/customers/:customer_id/contact/:medium_id",
            post(customers::update_contact),
        )
        .route(
            "/customers/:customer_id/contact/:medium_id/remove",
            post(customers::remove_contact),
        )
        .route(
            "/customers/:customer_id/close",
            post(customers::close_customer),
        )
        .route("/customers/:customer_id/case", post(customers::open_case))
        .nest_service("/static", ServeDir::new(templating::local_static_dir()))
        .nest_service(
            "/portal-ui/static",
            ServeDir::new(templating::shared_static_dir()),
        )
        .with_state(state)
}

/// Construct the [`AppState`] from the environment. Client construction is
/// best-effort — without a perimeter token the bundle is `None` and the CRM
/// screens degrade.
pub fn build_state() -> AppState {
    let settings = Settings::from_env();
    let clients = match CockpitClients::from_env(&settings) {
        Ok(c) => Some(Arc::new(c)),
        Err(e) => {
            tracing::warn!(error = %e, "cockpit.clients.unavailable");
            None
        }
    };
    let chat_registry = build_cockpit_registry(&settings).map(Arc::new);
    // v1.5 — fail-closed. The binary validates this at boot before build_state is
    // reached, so a bad value never gets this far; default defensively.
    let autonomy_mode = bss_orchestrator::read_autonomy_mode().unwrap_or(AutonomyMode::Granular);
    AppState {
        env: templating::build_environment(),
        settings: Arc::new(settings),
        clients,
        store: None,
        chat_registry,
        autonomy_mode,
        inflight: inflight::Inflight::new(),
    }
}

/// Build the `operator_cockpit` tool surface — the full registry minus the
/// customer-side `*.mine` wrappers.
///
/// This is the agent's own client bundle, deliberately separate from
/// [`CockpitClients`] — mirroring Python, where the orchestrator holds its own
/// `get_clients()` distinct from the portal's. `None` without a perimeter token.
fn build_cockpit_registry(settings: &Settings) -> Option<ToolRegistry> {
    use bss_clients::{
        CatalogClient, ComClient, CrmClient, InventoryClient, MediationClient, PaymentClient,
        ProvisioningClient, SomClient, SubscriptionClient,
    };
    use bss_orchestrator::tools;

    let auth = clients::cockpit_auth().ok()?;
    let catalog = CatalogClient::new(settings.catalog_url.clone(), auth.clone()).ok()?;
    let crm = CrmClient::new(settings.crm_url.clone(), auth.clone()).ok()?;
    let sub = SubscriptionClient::new(settings.subscription_url.clone(), auth.clone()).ok()?;
    let payment = PaymentClient::new(settings.payment_url.clone(), auth.clone()).ok()?;
    let com = ComClient::new(settings.com_url.clone(), auth.clone()).ok()?;
    let som = SomClient::new(settings.som_url.clone(), auth.clone()).ok()?;
    // Inventory lives inside CRM (same base URL).
    let inventory = InventoryClient::new(settings.crm_url.clone(), auth.clone()).ok()?;
    let provisioning =
        ProvisioningClient::new(settings.provisioning_url.clone(), auth.clone()).ok()?;
    let mediation = MediationClient::new(settings.mediation_url.clone(), auth.clone()).ok()?;

    let mut r = ToolRegistry::new();
    // Reads.
    tools::clock::register_clock_tools(&mut r);
    tools::catalog::register_catalog_tools(&mut r, catalog.clone());
    tools::customer::register_customer_tools(&mut r, crm.clone(), sub.clone());
    tools::case::register_case_tools(&mut r, crm.clone());
    tools::ticket::register_ticket_tools(&mut r, crm.clone());
    tools::port_request::register_port_request_tools(&mut r, crm.clone());
    tools::ops::register_ops_tools(&mut r, crm.clone());
    tools::subscription::register_subscription_tools(&mut r, sub.clone());
    tools::payment::register_payment_tools(&mut r, payment.clone());
    tools::order::register_order_tools(&mut r, com.clone());
    tools::som::register_som_tools(&mut r, som.clone());
    tools::inventory::register_inventory_tools(&mut r, inventory.clone());
    tools::provisioning::register_provisioning_tools(&mut r, provisioning.clone());
    tools::promo::register_promo_tools(&mut r, catalog.clone());
    tools::usage::register_usage_tools(&mut r, mediation.clone());
    // Writes — the cockpit is the operator surface, so it carries the full write
    // set. The destructive ones are gated by the loop's wrapper + /confirm, not
    // by omission from the registry.
    tools::customer::register_customer_write_tools(&mut r, crm.clone());
    tools::case::register_case_write_tools(&mut r, crm.clone());
    tools::ticket::register_ticket_write_tools(&mut r, crm.clone());
    tools::port_request::register_port_request_write_tools(&mut r, crm.clone());
    tools::subscription::register_subscription_write_tools(&mut r, sub.clone());
    tools::payment::register_payment_write_tools(&mut r, payment.clone());
    tools::order::register_order_write_tools(&mut r, com.clone());
    tools::inventory::register_inventory_write_tools(&mut r, inventory.clone());
    tools::provisioning::register_provisioning_write_tools(&mut r, provisioning.clone());
    tools::promo::register_promo_write_tools(&mut r, catalog.clone());
    tools::catalog::register_catalog_admin_write_tools(&mut r, catalog.clone());
    // NOTE: `trace.*` (JaegerClient + AuditClient) and `knowledge.*` (a PgPool)
    // need infra handles this bundle doesn't carry; they land with the CLI/REPL
    // wiring in P7, where the same registry is built once and shared.
    Some(r)
}

/// Like [`build_state`] but also connects the cockpit `Conversation` store.
///
/// Unlike the self-serve portal's optional pool, the cockpit **cannot run without
/// its store** — the whole surface is the conversation. Mirrors the Python
/// lifespan, which raises when `BSS_DB_URL` is unset.
pub async fn build_state_with_db() -> Result<AppState, String> {
    let mut state = build_state();
    if state.settings.db_url.is_empty() {
        return Err("BSS_DB_URL is unset; the operator cockpit cannot boot \
                    without its Conversation store."
            .to_string());
    }
    let pool = bss_db::connect(&state.settings.db_url)
        .await
        .map_err(|e| format!("cockpit store connect failed: {e}"))?;
    state.store = Some(Arc::new(ConversationStore::new(pool)));
    Ok(state)
}
