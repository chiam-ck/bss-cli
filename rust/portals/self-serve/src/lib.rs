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

pub mod account_reads;
pub mod account_writes;
pub mod auth;
pub mod clients;
pub mod config;
pub mod dashboard;
pub mod deps;
pub mod error_messages;
pub mod kyc;
pub mod middleware;
pub mod offerings;
pub mod payment_methods;
pub mod profile;
pub mod prompts;
pub mod qrpng;
pub mod routes;
pub mod security;
pub mod signup;
pub mod signup_session;
pub mod stepup;
pub mod templating;

use std::path::PathBuf;
use std::sync::Arc;

use axum::routing::{get, post};
use axum::Router;
use bss_portal_auth::EmailAdapter;
use minijinja::Environment;
use sqlx::PgPool;
use tower_http::services::ServeDir;

use clients::PortalClients;
use config::Settings;
use signup_session::SessionStore;

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
    /// Email delivery adapter (logging/noop/…), selected at boot. `None` only if
    /// selection failed (fail-fast at startup in the binary).
    pub email_adapter: Option<Arc<dyn EmailAdapter>>,
    /// TTL-bounded in-memory store of in-flight signup sessions.
    pub signup_store: Arc<SessionStore>,
    /// KYC verification adapter selected at boot (`prebaked` in dev/scenario).
    pub kyc_adapter: kyc::KycAdapter,
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
        .route("/", get(dashboard::dashboard))
        .route("/health", get(routes::health))
        .route("/welcome", get(routes::welcome))
        .route("/plans", get(routes::plans))
        .route("/terms", get(routes::terms))
        .route("/privacy", get(routes::privacy))
        .route("/branding/logo", get(routes::branding_logo))
        .route(
            "/auth/login",
            get(auth::login_form).post(auth::login_submit),
        )
        .route(
            "/auth/check-email",
            get(auth::check_email_form).post(auth::check_email_submit),
        )
        .route("/auth/verify", get(auth::verify_magic_link))
        .route("/auth/logout", post(auth::logout))
        .route(
            "/auth/step-up",
            get(stepup::step_up_form).post(stepup::step_up_verify),
        )
        .route("/auth/step-up/start", post(stepup::step_up_start))
        .route("/profile/contact", get(profile::contact_view))
        .route("/profile/contact/name/update", post(profile::name_update))
        .route("/profile/contact/phone/update", post(profile::phone_update))
        .route(
            "/profile/contact/address/update",
            post(profile::address_update),
        )
        .route(
            "/profile/contact/email/change",
            post(profile::email_change_start),
        )
        .route(
            "/profile/contact/email/verify",
            get(profile::email_change_verify_form).post(profile::email_change_verify_submit),
        )
        .route(
            "/profile/contact/email/cancel",
            post(profile::email_change_cancel),
        )
        .route("/payment-methods", get(payment_methods::list_methods))
        .route(
            "/payment-methods/add",
            get(payment_methods::add_method_form).post(payment_methods::add_method),
        )
        .route(
            "/payment-methods/:pm_id/remove",
            post(payment_methods::remove_method),
        )
        .route(
            "/payment-methods/:pm_id/set-default",
            post(payment_methods::set_default),
        )
        .route("/billing/history", get(account_reads::history))
        .route("/esim/:subscription_id", get(account_reads::esim_view))
        .route(
            "/plan/change",
            get(account_writes::plan_change_form).post(account_writes::plan_change_submit),
        )
        .route(
            "/plan/change/cancel",
            post(account_writes::plan_change_cancel),
        )
        .route(
            "/plan/change/scheduled",
            get(account_writes::plan_change_scheduled),
        )
        .route(
            "/subscription/:subscription_id/cancel",
            get(account_writes::cancel_confirm).post(account_writes::cancel_submit),
        )
        .route(
            "/subscription/:subscription_id/cancelled",
            get(account_writes::cancel_success),
        )
        .route(
            "/top-up",
            get(account_writes::top_up_form).post(account_writes::top_up_submit),
        )
        .route("/top-up/success", get(account_writes::top_up_success))
        .route("/api/session/:session_id", get(signup::session_status))
        .route("/signup", post(signup::signup_submit))
        .route("/signup/promo/preview", get(signup::signup_promo_preview))
        .route("/signup/step/kyc", post(signup::signup_step_kyc))
        .route("/signup/step/cof", post(signup::signup_step_cof))
        .route("/signup/step/order", post(signup::signup_step_order))
        .route("/signup/step/poll", get(signup::signup_step_poll))
        .route("/signup/:plan_id", get(signup::signup_form))
        .route("/signup/:plan_id/msisdn", get(signup::msisdn_picker))
        .route("/signup/:plan_id/progress", get(signup::signup_progress))
        .route("/confirmation/:subscription_id", get(signup::confirmation))
        .route("/activation/:order_id", get(signup::activation))
        .route(
            "/activation/:order_id/status",
            get(signup::activation_status),
        )
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
    let email_adapter = select_email_adapter();
    let signup_ttl = settings.session_ttl.max(0) as u64;
    let kyc_adapter = kyc::KycAdapter::from_provider(&settings.kyc_provider);
    AppState {
        env: templating::build_environment(),
        settings: Arc::new(settings),
        clients,
        db: None,
        email_adapter,
        signup_store: Arc::new(SessionStore::new(signup_ttl)),
        kyc_adapter,
    }
}

/// Select the email adapter from `BSS_PORTAL_EMAIL_PROVIDER` (legacy
/// `BSS_PORTAL_EMAIL_ADAPTER` fallback). Best-effort here (`None` on error);
/// the binary treats a hard failure as fatal at boot.
fn select_email_adapter() -> Option<Arc<dyn EmailAdapter>> {
    let auth = bss_portal_auth::Settings::from_env();
    let provider =
        bss_portal_auth::resolve_provider_name(&auth.email_provider, &auth.email_adapter);
    match bss_portal_auth::select_adapter(
        &provider,
        &auth.dev_mailbox_path,
        &auth.email_resend_api_key,
        &auth.email_from,
    ) {
        Ok(a) => Some(Arc::from(a)),
        Err(e) => {
            tracing::warn!(error = %e, provider = %provider, "portal.email_adapter.unavailable");
            None
        }
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
