//! som entrypoint — port of `app.__main__` + `app.dependencies.lifespan`.
//!
//! Boot order mirrors the Python lifespan: fail-fast on token misconfig, init
//! telemetry, connect the pool, build the Inventory client, then (best-effort) the
//! four safe consumers + the outbox relay — each on its own MQ connection like the
//! Python service — then serve on 8000.

use std::collections::BTreeMap;
use std::sync::Arc;

use bss_clients::{InventoryClient, TokenAuthProvider};
use bss_events::{start_relay, MqChannel};
use bss_middleware::validate_token_map_present;
use som::config::Settings;
use som::state::AppState;
use tokio::net::TcpListener;

type MainError = Box<dyn std::error::Error>;

#[tokio::main]
async fn main() -> Result<(), MainError> {
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

    let auth = Arc::new(TokenAuthProvider::new(settings.api_token.clone())?);
    let inventory = InventoryClient::new(settings.crm_url.clone(), auth)?;

    // Consumers + relay are best-effort (a missing broker still serves HTTP; the
    // durable audit log records everything for later replay).
    let mut _relay = None;
    if settings.mq_url.is_empty() {
        tracing::warn!("mq.not_configured");
    } else {
        // Consumer connection (declares the retry exchange, then binds 4 queues).
        match MqChannel::connect(&settings.mq_url).await {
            Ok(c) => {
                let mq = Arc::new(c);
                if let Err(e) = mq.declare_retry_exchange().await {
                    tracing::warn!(error = %e, "mq.retry_exchange.declare_failed");
                }
                som::consumer::spawn_consumers(
                    mq,
                    pool.clone(),
                    inventory.clone(),
                    settings.mq_max_retries,
                    settings.mq_retry_backoff_ms,
                );
            }
            Err(e) => tracing::warn!(error = %e, "mq.consumer.setup_failed"),
        }

        // Relay connection (the single publisher of staged events).
        match MqChannel::connect(&settings.mq_url).await {
            Ok(c) => {
                _relay = Some(start_relay(
                    pool.clone(),
                    Arc::new(c),
                    settings.outbox_relay_interval_ms,
                    settings.outbox_relay_batch_size,
                ));
            }
            Err(e) => tracing::warn!(error = %e, "outbox.relay.setup_failed"),
        }
    }

    let state = AppState {
        pool,
        inventory,
        settings: settings.clone(),
    };
    let app = som::create_app(state, token_map);

    let listener = TcpListener::bind("0.0.0.0:8000").await?;
    tracing::info!(service = settings.service_name, "service.starting");
    axum::serve(listener, app).await?;
    Ok(())
}
