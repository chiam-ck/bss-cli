//! bss-self-serve — the customer self-serve portal (service 9001). Rust port of
//! `portals/self-serve`.
//!
//! **Slice 1 (this):** the axum app skeleton + branding-aware MiniJinja
//! templating (reusing the existing Jinja templates via [`templating`]) + the
//! **public static surface**: `/health`, `/welcome`, `/terms`, `/privacy`,
//! `/branding/logo`, and the `/static` + `/portal-ui/static` mounts. These need
//! no BSS read and no session, so they prove the whole render stack end-to-end.
//!
//! **Following slices:** `/plans` (first catalog read), the session middleware
//! (tower layer) + `bss-portal-auth` DB session layer, the auth/login flow, the
//! signup + KYC funnel, the post-login account surface, and the SSE chat route.
#![forbid(unsafe_code)]

pub mod clients;
pub mod config;
pub mod middleware;
pub mod offerings;
pub mod routes;
pub mod security;
pub mod templating;

use std::path::PathBuf;
use std::sync::Arc;

use axum::routing::get;
use axum::Router;
use minijinja::Environment;
use sqlx::PgPool;
use tower_http::services::ServeDir;

use clients::PortalClients;
use config::Settings;

/// Shared application state (cheap to clone — everything behind `Arc`).
#[derive(Clone)]
pub struct AppState {
    pub env: Arc<Environment<'static>>,
    pub settings: Arc<Settings>,
    /// `None` when the perimeter token isn't provisioned (e.g. template-only
    /// tests); catalog-backed routes degrade to an empty view.
    pub clients: Option<Arc<PortalClients>>,
    /// `portal_auth` pool for session resolution. `None` without `BSS_DB_URL`;
    /// the session middleware then resolves every request as anonymous.
    pub db: Option<PgPool>,
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.."))
}

fn local_static_dir() -> PathBuf {
    match std::env::var("BSS_PORTAL_STATIC_DIR") {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => repo_root().join("portals/self-serve/bss_self_serve/static"),
    }
}

fn shared_static_dir() -> PathBuf {
    match std::env::var("BSS_PORTAL_SHARED_STATIC_DIR") {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => repo_root().join("packages/bss-portal-ui/bss_portal_ui/static"),
    }
}

/// Build the portal router with all routes + the session middleware + static
/// mounts.
pub fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(routes::health))
        .route("/welcome", get(routes::welcome))
        .route("/plans", get(routes::plans))
        .route("/terms", get(routes::terms))
        .route("/privacy", get(routes::privacy))
        .route("/branding/logo", get(routes::branding_logo))
        .nest_service("/static", ServeDir::new(local_static_dir()))
        .nest_service("/portal-ui/static", ServeDir::new(shared_static_dir()))
        // Session middleware runs on every request, resolving the cookie →
        // `PortalSession` extension (anon when absent). Layer wraps the routes.
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            middleware::session_layer,
        ))
        .with_state(state)
}

/// Construct the [`AppState`] from the environment: the MiniJinja env, the
/// settings, and the downstream clients bundle. Client construction is
/// best-effort — without a perimeter token the bundle is `None` and
/// catalog-backed routes degrade to an empty view.
pub fn build_state() -> AppState {
    let settings = Settings::from_env();
    let clients = match PortalClients::from_env(&settings) {
        Ok(c) => Some(Arc::new(c)),
        Err(e) => {
            tracing::warn!(error = %e, "portal.clients.unavailable");
            None
        }
    };
    AppState {
        env: templating::build_environment(),
        settings: Arc::new(settings),
        clients,
        db: None,
    }
}

/// Like [`build_state`] but also connects the `portal_auth` pool (from
/// `BSS_DB_URL`) so the session middleware can resolve cookies. Async because
/// the pool connects; used by the binary. Falls back to `db: None` if unset or
/// the connection fails (the portal still serves public pages).
pub async fn build_state_with_db() -> AppState {
    let mut state = build_state();
    if !state.settings.db_url.is_empty() {
        match bss_db::connect(&state.settings.db_url).await {
            Ok(pool) => state.db = Some(pool),
            Err(e) => tracing::warn!(error = %e, "portal.db.connect_failed"),
        }
    } else {
        tracing::warn!("portal.db_url.missing");
    }
    state
}
