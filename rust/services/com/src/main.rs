//! com entrypoint — port of `app.__main__` + `app.dependencies.lifespan`.
//!
//! Boot order mirrors the Python lifespan: fail-fast on token misconfig, init
//! telemetry, connect the pool, build the six S2S clients (loyalty optional),
//! then (best-effort) the two safe consumers + the outbox relay — each on its own
//! MQ connection — and the reconciliation sweeper, then serve on 8000.

use std::collections::BTreeMap;
use std::sync::Arc;

use bss_clients::{
    BearerAuthProvider, CatalogClient, CrmClient, LoyaltyClient, PaymentClient, SomClient,
    SubscriptionClient, TokenAuthProvider,
};
use bss_events::{start_relay, MqChannel};
use bss_middleware::validate_token_map_present;
use com::config::Settings;
use com::state::AppState;
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

    let auth = || -> Result<Arc<TokenAuthProvider>, MainError> {
        Ok(Arc::new(TokenAuthProvider::new(
            settings.api_token.clone(),
        )?))
    };
    let crm = CrmClient::new(settings.crm_url.clone(), auth()?)?;
    let catalog = CatalogClient::new(settings.catalog_url.clone(), auth()?)?;
    let payment = PaymentClient::new(settings.payment_url.clone(), auth()?)?;
    let som = SomClient::new(settings.som_url.clone(), auth()?)?;
    let subscription = SubscriptionClient::new(settings.subscription_url.clone(), auth()?)?;
    let loyalty = if settings.loyalty_api_token.is_empty() {
        tracing::warn!(
            reason = "BSS_LOYALTY_API_TOKEN unset",
            "com.loyalty.disabled"
        );
        None
    } else {
        let bearer = Arc::new(BearerAuthProvider::new(settings.loyalty_api_token.clone())?);
        Some(LoyaltyClient::new(
            settings.loyalty_base_url.clone(),
            bearer,
        )?)
    };

    // Consumers + relay + sweeper are best-effort (a missing broker still serves
    // HTTP; the durable audit log records everything for later replay).
    let mut _relay = None;
    if settings.mq_url.is_empty() {
        tracing::warn!("mq.not_configured");
    } else {
        match MqChannel::connect(&settings.mq_url).await {
            Ok(c) => {
                let mq = Arc::new(c);
                if let Err(e) = mq.declare_retry_exchange().await {
                    tracing::warn!(error = %e, "mq.retry_exchange.declare_failed");
                }
                com::consumer::spawn_consumers(
                    mq,
                    pool.clone(),
                    subscription.clone(),
                    loyalty.clone(),
                    settings.mq_max_retries,
                    settings.mq_retry_backoff_ms,
                );
            }
            Err(e) => tracing::warn!(error = %e, "mq.consumer.setup_failed"),
        }
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

    // Reconciliation sweeper (0 disables — tests).
    if settings.reconciliation_interval_seconds > 0 {
        let pool = pool.clone();
        let threshold = settings.order_stuck_threshold_seconds;
        let interval = settings.reconciliation_interval_seconds;
        tokio::spawn(async move {
            com::reconciliation::tick_loop(pool, threshold, interval).await;
        });
    }

    let state = AppState {
        pool,
        crm,
        catalog,
        payment,
        som,
        subscription,
        loyalty,
        settings: settings.clone(),
    };
    let app = com::create_app(state, token_map);

    let listener = TcpListener::bind("0.0.0.0:8000").await?;
    tracing::info!(service = settings.service_name, "service.starting");
    axum::serve(listener, app).await?;
    Ok(())
}
