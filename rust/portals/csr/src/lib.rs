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
//! The portal is fully ported. It carries the app skeleton (config, branding-aware
//! MiniJinja templating over the existing Jinja templates, static mounts,
//! `/health`), the shared [`views`] payload helpers, the ASCII renderers (in
//! `bss-cockpit`), the cockpit chat thread with its SSE + `/confirm` flow, the full
//! v1.6 CRM surface across [`customers`], [`cases`], [`orders`], [`catalog`],
//! [`subscriptions`] and [`search`] (the v1.6.1 two-step confirm test-pinned over
//! all ten destructive verbs in `tests/routes_crm.rs`), the [`handoff`] "Ask the
//! agent" seam, and the operator [`settings`] + [`branding`] editors backed by the
//! `bss_cockpit` config writers.
#![forbid(unsafe_code)]

pub mod branding;
pub mod bubble;
pub mod cases;
pub mod catalog;
pub mod clients;
pub mod cockpit;
pub mod config;
pub mod customers;
pub mod guards;
pub mod handoff;
pub mod inflight;
pub mod orders;
pub mod routes;
pub mod search;
pub mod sessions;
pub mod settings;
pub mod subscriptions;
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
        // Cases — queue + thread + workbench (v1.6).
        .route("/cases", get(cases::cases_list))
        .route("/case/:case_id", get(cases::case_thread))
        .route("/case/:case_id/close", post(cases::case_close))
        .route("/case/:case_id/note", post(cases::case_add_note))
        .route("/case/:case_id/transition", post(cases::case_transition))
        .route("/case/:case_id/priority", post(cases::case_priority))
        .route("/case/:case_id/ticket", post(cases::case_open_ticket))
        .route(
            "/case/:case_id/ticket/:ticket_id/assign",
            post(cases::ticket_assign),
        )
        .route(
            "/case/:case_id/ticket/:ticket_id/transition",
            post(cases::ticket_transition),
        )
        .route(
            "/case/:case_id/ticket/:ticket_id/resolve",
            post(cases::ticket_resolve),
        )
        .route(
            "/case/:case_id/ticket/:ticket_id/cancel",
            post(cases::ticket_cancel),
        )
        // Orders — queue + create/jump + COM/SOM detail + submit/cancel (v1.6).
        // Static segments (create/jump) registered before the `:order_id` param.
        .route("/orders", get(orders::orders_list))
        .route("/orders/create", post(orders::create_order))
        .route("/orders/jump", get(orders::orders_jump))
        .route("/orders/:order_id", get(orders::order_detail))
        .route("/orders/:order_id/submit", post(orders::submit_order))
        .route("/orders/:order_id/cancel", post(orders::cancel_order))
        // Catalog — plans/VAS/promos index + admin CRUD + offering detail (v1.6).
        // Static `offering` before the `:offering_id` param.
        .route("/catalog", get(catalog::catalog_index))
        .route("/catalog/offering", post(catalog::add_offering))
        .route("/catalog/:offering_id", get(catalog::offering_detail))
        .route("/catalog/:offering_id/price", post(catalog::add_price))
        .route("/catalog/:offering_id/window", post(catalog::set_window))
        .route(
            "/catalog/:offering_id/retire",
            post(catalog::retire_offering),
        )
        // Subscriptions — detail + lifecycle CRUD (v1.6). Nested under the
        // customers nav (active_page="customers"); no separate list screen.
        .route(
            "/subscriptions/:subscription_id",
            get(subscriptions::subscription_detail),
        )
        .route(
            "/subscriptions/:subscription_id/plan-change",
            post(subscriptions::schedule_plan_change),
        )
        .route(
            "/subscriptions/:subscription_id/plan-change/cancel",
            post(subscriptions::cancel_plan_change),
        )
        .route(
            "/subscriptions/:subscription_id/renew",
            post(subscriptions::renew_now),
        )
        .route(
            "/subscriptions/:subscription_id/vas",
            post(subscriptions::purchase_vas),
        )
        .route(
            "/subscriptions/:subscription_id/terminate",
            post(subscriptions::terminate),
        )
        // Search — customer lookup + jump into a pinned cockpit session (v1.6).
        .route("/search", get(search::search))
        .route("/search/start_session", post(search::start_session))
        // Handoff — "Ask the agent" from any CRM screen (v1.6).
        .route("/cockpit/handoff", post(handoff::cockpit_handoff))
        // Settings — OPERATOR.md + settings.toml editors (v0.13 PR8).
        .route("/settings", get(settings::settings_page))
        .route("/settings/operator", post(settings::save_operator_md))
        .route("/settings/config", post(settings::save_config_toml))
        // Branding — brand name / theme / mark / logo (v1.8). Static segments
        // before nothing here (all fixed), but ordered for readability.
        .route(
            "/settings/branding",
            get(branding::branding_page).post(branding::branding_save),
        )
        .route(
            "/settings/branding/logo",
            post(branding::branding_logo_upload),
        )
        .route(
            "/settings/branding/logo/delete",
            post(branding::branding_logo_delete),
        )
        .route(
            "/settings/branding/preview",
            get(branding::branding_preview),
        )
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
        AuditClient, CatalogClient, ComClient, CrmClient, InventoryClient, JaegerClient,
        MediationClient, PaymentClient, ProvisioningClient, SomClient, SubscriptionClient,
    };
    use bss_orchestrator::{build_registry, RegistryClients, RegistryExtras};

    let auth = clients::cockpit_auth().ok()?;
    let reg_clients = RegistryClients {
        catalog: CatalogClient::new(settings.catalog_url.clone(), auth.clone()).ok()?,
        crm: CrmClient::new(settings.crm_url.clone(), auth.clone()).ok()?,
        // Inventory lives inside CRM (same base URL).
        inventory: InventoryClient::new(settings.crm_url.clone(), auth.clone()).ok()?,
        payment: PaymentClient::new(settings.payment_url.clone(), auth.clone()).ok()?,
        com: ComClient::new(settings.com_url.clone(), auth.clone()).ok()?,
        som: SomClient::new(settings.som_url.clone(), auth.clone()).ok()?,
        subscription: SubscriptionClient::new(settings.subscription_url.clone(), auth.clone())
            .ok()?,
        mediation: MediationClient::new(settings.mediation_url.clone(), auth.clone()).ok()?,
        provisioning: ProvisioningClient::new(settings.provisioning_url.clone(), auth.clone())
            .ok()?,
    };

    // `trace.*` — the `operator_cockpit` profile lists these, so the cockpit carries
    // them. Jaeger is unauthenticated; the audit surfaces reuse the cockpit token.
    // `knowledge.*` needs the FTS pool, which isn't connected at this sync build
    // point (it lands in `build_state_with_db`); the cockpit's knowledge wiring
    // follows there. `bss ask` / the REPL supply the pool directly.
    let extras = RegistryExtras {
        jaeger: JaegerClient::from_env().ok(),
        audit_com: AuditClient::new(settings.com_url.clone(), auth.clone()).ok(),
        audit_sub: AuditClient::new(settings.subscription_url.clone(), auth.clone()).ok(),
        knowledge_pool: None,
    };

    Some(build_registry(&reg_clients, extras))
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
