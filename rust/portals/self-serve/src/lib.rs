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
pub mod offerings;
pub mod routes;
pub mod templating;

use std::path::PathBuf;
use std::sync::Arc;

use axum::routing::get;
use axum::Router;
use minijinja::Environment;
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

/// Build the portal router with all slice-1 routes + static mounts.
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
    }
}
