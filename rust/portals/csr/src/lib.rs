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
//! **Slice 1 (this):** the app skeleton — config, branding-aware MiniJinja
//! templating (reusing the existing Jinja templates), the static mounts, `/health`,
//! and [`views`] (the shared snake_case/camelCase-lenient payload helpers every
//! later screen reads through).
//!
//! **Following slices:** the ASCII renderers (the P5b debt), the cockpit chat
//! thread + SSE + `/confirm`, then the CRM screens (customers / cases / orders /
//! catalog / subscriptions / search), settings + branding + handoff.
#![forbid(unsafe_code)]

pub mod bubble;
pub mod clients;
pub mod config;
pub mod guards;
pub mod routes;
pub mod sessions;
pub mod templating;
pub mod turn;
pub mod views;

use std::sync::Arc;

use axum::routing::get;
use axum::Router;
use bss_cockpit::ConversationStore;
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
}

/// Build the cockpit router.
pub fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(routes::health))
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
    AppState {
        env: templating::build_environment(),
        settings: Arc::new(settings),
        clients,
        store: None,
    }
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
