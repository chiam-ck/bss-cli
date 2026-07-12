//! mediation service entrypoint — port of `app.__main__` + `app.dependencies.lifespan`.
//!
//! Boot order mirrors the Python lifespan: fail-fast on token misconfig, init
//! telemetry, connect the pool, build the Subscription client, connect MQ
//! (best-effort — mediation only *publishes*, no consumer), then serve on 8000.

use std::collections::BTreeMap;
use std::sync::Arc;

use bss_clients::{SubscriptionClient, TokenAuthProvider};
use bss_events::MqChannel;
use bss_middleware::validate_token_map_present;
use mediation::config::Settings;
use mediation::state::AppState;
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
    let subscription = SubscriptionClient::new(settings.subscription_url.clone(), auth)?;

    // MQ is best-effort: mediation publishes usage.recorded / usage.rejected. A
    // missing broker stages the audit row without publishing (Python
    // "mq.not_configured" / "mq.connect.failed").
    let mq = if settings.mq_url.is_empty() {
        tracing::warn!("mq.not_configured");
        None
    } else {
        match MqChannel::connect(&settings.mq_url).await {
            Ok(c) => {
                tracing::info!(exchange = "bss.events", "mq.connected");
                Some(Arc::new(c))
            }
            Err(e) => {
                tracing::warn!(error = %e, "mq.connect.failed");
                None
            }
        }
    };

    let state = AppState {
        pool,
        subscription,
        settings: settings.clone(),
        mq,
    };
    let app = mediation::create_app(state, token_map);

    let listener = TcpListener::bind("0.0.0.0:8000").await?;
    tracing::info!(service = settings.service_name, "service.starting");
    axum::serve(listener, app).await?;
    Ok(())
}
