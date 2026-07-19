//! provisioning-sim entrypoint — port of `app.__main__` + `app.dependencies.lifespan`.
//!
//! Boot order mirrors the Python lifespan: fail-fast on token misconfig, init
//! telemetry, connect the pool, resolve the eSIM provider (fail-fast on an
//! unknown name; `onbglobal`/`esim_access` are accepted here and raise on first
//! use), connect MQ + spawn the `provisioning.task.created` consumer
//! (best-effort), then serve on 8000.

use std::collections::BTreeMap;
use std::sync::Arc;

use bss_events::MqChannel;
use bss_middleware::validate_token_map_present;
use provisioning_sim::config::Settings;
use provisioning_sim::esim::select_esim_provider;
use provisioning_sim::state::AppState;
use tokio::net::TcpListener;

type MainError = Box<dyn std::error::Error>;

#[tokio::main]
async fn main() -> Result<(), MainError> {
    // Distroless healthcheck self-probe: `--healthcheck` exits before any bootstrap.
    bss_telemetry::maybe_run_healthcheck(8000);

    let settings = Settings::from_env();
    let _telemetry = bss_telemetry::init_telemetry(&settings.service_name);

    let env: BTreeMap<String, String> = std::env::vars().collect();
    let token_map = Arc::new(
        validate_token_map_present(&env)
            .map_err(|e| format!("BSS API token misconfigured: {e:?}"))?,
    );

    if settings.db_url.is_empty() {
        return Err("BSS_DB_URL is not set".into());
    }
    let pool = bss_db::connect(&settings.db_url).await?;

    // Fail-fast on an unknown provider name (Python `select_esim_provider` raises).
    let esim = select_esim_provider(&settings.esim_provider)?;
    tracing::info!(name = settings.esim_provider, "esim_provider.selected");

    // MQ is best-effort: no broker → no consumer, HTTP still serves.
    let mq = if settings.mq_url.is_empty() {
        tracing::warn!("mq.not_configured");
        None
    } else {
        match MqChannel::connect(&settings.mq_url).await {
            Ok(c) => Some(Arc::new(c)),
            Err(e) => {
                tracing::warn!(error = %e, "mq.consumer.setup_failed");
                None
            }
        }
    };

    if let Some(mq) = mq.clone() {
        let pool = pool.clone();
        tokio::spawn(async move {
            if let Err(e) = provisioning_sim::consumer::run(mq, pool, esim).await {
                tracing::error!(error = %e, "mq.consumer.stopped");
            }
        });
    }

    let state = AppState {
        pool,
        settings: settings.clone(),
        esim,
        mq,
    };
    let app = provisioning_sim::create_app(state, token_map);

    let listener = TcpListener::bind("0.0.0.0:8000").await?;
    tracing::info!(service = settings.service_name, "service.starting");
    axum::serve(listener, app).await?;
    Ok(())
}
