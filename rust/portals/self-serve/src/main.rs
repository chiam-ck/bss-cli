//! portal-self-serve entrypoint. Port of `bss_self_serve.main` (slice 1).
//!
//! Boot: init telemetry, build the MiniJinja env + settings, serve on the
//! configured port (default 9001). The v0.8 lifespan's pepper validation, DB
//! engine, email/KYC/payment adapter selection, and the session middleware land
//! in following slices.

use bss_self_serve::{build_router, build_state_with_db};
use tokio::net::TcpListener;

type MainError = Box<dyn std::error::Error>;

#[tokio::main]
async fn main() -> Result<(), MainError> {
    // v0.8 — fail-fast on the token pepper BEFORE any auth flow can run (mirrors
    // the Python lifespan). The login flow's HMAC relies on it.
    bss_portal_auth::validate_pepper_present()
        .map_err(|e| format!("BSS_PORTAL_TOKEN_PEPPER misconfigured: {e}"))?;

    let state = build_state_with_db().await;
    let _telemetry = bss_telemetry::init_telemetry(&state.settings.service_name);

    let port = state.settings.port;
    let router = build_router(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = TcpListener::bind(&addr).await?;
    tracing::info!(service = "portal-self-serve", %addr, "portal.starting");
    axum::serve(listener, router).await?;
    Ok(())
}
