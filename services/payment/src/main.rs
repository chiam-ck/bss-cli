//! payment entrypoint — port of `app.__main__` + `app.dependencies.lifespan`.
//!
//! Boot order mirrors the Python lifespan: fail-fast on token misconfig, init
//! telemetry, connect the pool, build the CRM client, then resolve the tokenizer
//! seam via `select_tokenizer` (fail-fast on any provider misconfig — unknown
//! provider, missing stripe creds, sk_test_* in production, ALLOW_TEST_CARD_REUSE
//! + sk_live_*), and serve on 8000. No MQ.

use std::collections::BTreeMap;
use std::sync::Arc;

use bss_clients::{CrmClient, TokenAuthProvider};
use bss_middleware::validate_token_map_present;
use payment::config::Settings;
use payment::select::select_tokenizer;
use payment::state::AppState;
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

    let crm = CrmClient::new(
        settings.crm_url.clone(),
        Arc::new(TokenAuthProvider::new(settings.api_token.clone())?),
    )?;

    // v0.16 tokenizer seam — resolved once, fail-fast on misconfig (never a
    // silent downgrade). The service refuses to boot on a bad provider config.
    let tokenizer = select_tokenizer(&settings, &pool)
        .map_err(|e| format!("payment provider misconfigured: {e}"))?;

    tracing::info!(
        service = settings.service_name,
        payment_provider = settings.payment_provider,
        "service.starting"
    );

    let state = AppState {
        pool,
        crm,
        tokenizer,
        settings: settings.clone(),
    };
    let app = payment::create_app(state, token_map);

    let listener = TcpListener::bind("0.0.0.0:8000").await?;
    axum::serve(listener, app).await?;
    Ok(())
}
