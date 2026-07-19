//! Shared application state — the axum equivalent of `app.state.*`.

use std::sync::Arc;

use bss_db::PgPool;
use bss_events::MqChannel;

use crate::config::Settings;
use crate::esim::EsimProvider;

/// Injected into every provisioning route handler via `State<AppState>`.
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub settings: Settings,
    /// The resolved eSIM adapter (`Copy` enum — the worker uses it per task).
    pub esim: EsimProvider,
    /// Present only when `BSS_MQ_URL` is configured — the inline-publish handle
    /// the worker uses for `provisioning.task.*`.
    pub mq: Option<Arc<MqChannel>>,
}
