//! crm entrypoint — port of `app.__main__` + `app.dependencies.lifespan`.
//!
//! Boot: fail-fast on token misconfig, init telemetry, connect the pool, build the
//! subscription client + optional loyalty client, serve on 8000. No broker — crm is
//! HTTP-only + stage-only (the oracle's lifespan opens no MQ).

use std::collections::BTreeMap;
use std::sync::Arc;

use bss_clients::{BearerAuthProvider, LoyaltyClient, SubscriptionClient, TokenAuthProvider};
use bss_middleware::validate_token_map_present;
use crm::config::Settings;
use crm::state::AppState;
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

    let subscription = SubscriptionClient::new(
        settings.subscription_url.clone(),
        Arc::new(TokenAuthProvider::new(settings.api_token.clone())?),
    )?;

    let loyalty = if settings.loyalty_api_token.is_empty() {
        tracing::warn!(
            reason = "BSS_LOYALTY_API_TOKEN unset",
            "crm.loyalty.disabled"
        );
        None
    } else {
        let bearer = Arc::new(BearerAuthProvider::new(settings.loyalty_api_token.clone())?);
        Some(LoyaltyClient::new(
            settings.loyalty_base_url.clone(),
            bearer,
        )?)
    };

    let state = AppState {
        pool,
        subscription,
        loyalty,
        settings: settings.clone(),
    };
    let app = crm::create_app(state, token_map);

    let listener = TcpListener::bind("0.0.0.0:8000").await?;
    tracing::info!(service = settings.service_name, "service.starting");
    axum::serve(listener, app).await?;
    Ok(())
}
