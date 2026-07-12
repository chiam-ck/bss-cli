//! catalog service entrypoint — port of `bss_catalog.__main__` + `deps.lifespan`.
//!
//! Boot order mirrors the Python lifespan: fail-fast on token misconfig, init
//! telemetry, connect the pool, build the **optional** loyalty client (OFF when
//! `BSS_LOYALTY_API_TOKEN` is unset), then serve on 8000. Catalog has no MQ.

use std::collections::BTreeMap;
use std::sync::Arc;

use bss_clients::{BearerAuthProvider, LoyaltyClient};
use bss_middleware::validate_token_map_present;
use catalog::config::Settings;
use catalog::state::AppState;
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

    // loyalty is OPTIONAL: unset token → promo subsystem OFF (catalog still
    // serves the rest). The bearer token never leaves this process.
    let loyalty = if settings.loyalty_api_token.is_empty() {
        tracing::warn!(
            reason = "BSS_LOYALTY_API_TOKEN unset",
            "catalog.loyalty.disabled"
        );
        None
    } else {
        let auth = Arc::new(BearerAuthProvider::new(settings.loyalty_api_token.clone())?);
        Some(LoyaltyClient::new(settings.loyalty_base_url.clone(), auth)?)
    };

    let state = AppState {
        pool,
        loyalty,
        settings: settings.clone(),
    };
    let app = catalog::create_app(state, token_map);

    let listener = TcpListener::bind("0.0.0.0:8000").await?;
    tracing::info!(service = settings.service_name, "service.starting");
    axum::serve(listener, app).await?;
    Ok(())
}
