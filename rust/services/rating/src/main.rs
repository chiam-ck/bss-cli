//! rating service entrypoint — port of `app.__main__` + `app.dependencies.lifespan`.
//!
//! Boot order mirrors the Python lifespan: fail-fast on token misconfig, init
//! telemetry, connect the pool, build the catalog client, connect MQ + spawn the
//! consumer (best-effort — a missing broker doesn't gate HTTP), then serve on 8000.

use std::collections::BTreeMap;
use std::sync::Arc;

use bss_clients::{CatalogClient, TokenAuthProvider};
use bss_events::MqChannel;
use bss_middleware::validate_token_map_present;
use rating::config::Settings;
use rating::state::AppState;
use tokio::net::TcpListener;

type MainError = Box<dyn std::error::Error>;

#[tokio::main]
async fn main() -> Result<(), MainError> {
    // Distroless healthcheck self-probe: `--healthcheck` exits before any bootstrap.
    bss_telemetry::maybe_run_healthcheck(8000);

    let settings = Settings::from_env();
    // Held for the process lifetime; flushes queued spans on drop.
    let _telemetry = bss_telemetry::init_telemetry(&settings.service_name);

    // Fail-fast on perimeter-token misconfig (Python `validate_api_token_present`).
    let env: BTreeMap<String, String> = std::env::vars().collect();
    let token_map = Arc::new(
        validate_token_map_present(&env)
            .map_err(|e| format!("BSS API token misconfigured: {e:?}"))?,
    );

    if settings.db_url.is_empty() {
        return Err("BSS_DB_URL is not set".into());
    }
    let pool = bss_db::connect(&settings.db_url).await?;

    let auth = Arc::new(TokenAuthProvider::new(settings.api_token.clone())?);
    let catalog = CatalogClient::new(settings.catalog_url.clone(), auth)?;

    // MQ is best-effort: no broker → no consumer, HTTP still serves (Python
    // catches consumer setup failures and logs `mq.consumer.setup_failed`).
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
        let catalog = catalog.clone();
        tokio::spawn(async move {
            if let Err(e) = rating::consumer::run(mq, pool, catalog).await {
                tracing::error!(error = %e, "mq.consumer.stopped");
            }
        });
    }

    let state = AppState {
        pool,
        catalog,
        settings: settings.clone(),
        mq,
    };
    let app = rating::create_app(state, token_map);

    let listener = TcpListener::bind("0.0.0.0:8000").await?;
    tracing::info!(service = settings.service_name, "service.starting");
    axum::serve(listener, app).await?;
    Ok(())
}
