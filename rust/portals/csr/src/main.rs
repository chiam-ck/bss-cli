//! portal-csr entrypoint. Port of `bss_csr.main`'s app factory + lifespan.
//!
//! Boot order mirrors the Python lifespan exactly:
//! 1. **telemetry first**, so every later boot warning is actually visible;
//! 2. `read_autonomy_mode()` — **fail-closed**: an unrecognised
//!    `BSS_REPL_LLM_AUTONOMY` refuses the boot rather than silently defaulting
//!    (same shape as the v0.9 named-token sentinel rejection);
//! 3. the cockpit `Conversation` store — the cockpit cannot run without it.
//!
//! No login and no inbound token middleware: single-operator-by-design behind a
//! secure perimeter (DECISIONS 2026-05-01).

use bss_csr::{build_router, build_state_with_db};
use tokio::net::TcpListener;

type MainError = Box<dyn std::error::Error>;

#[tokio::main]
async fn main() -> Result<(), MainError> {
    // Telemetry BEFORE state construction — otherwise the store/client boot
    // warnings are emitted before a subscriber exists and vanish.
    let settings = bss_csr::config::Settings::from_env();
    let _telemetry = bss_telemetry::init_telemetry(&settings.service_name);

    // v1.5 — fail-closed autonomy validation. Let the error end the process.
    let autonomy = bss_orchestrator::read_autonomy_mode()
        .map_err(|e| format!("BSS_REPL_LLM_AUTONOMY misconfigured: {e}"))?;

    let state = build_state_with_db().await?;

    let port = state.settings.port;
    let router = build_router(state);

    let addr = format!("0.0.0.0:{port}");
    let listener = TcpListener::bind(&addr).await?;
    tracing::info!(
        service = "portal-csr",
        %addr,
        autonomy_mode = ?autonomy,
        "cockpit.starting"
    );
    axum::serve(listener, router).await?;
    Ok(())
}
