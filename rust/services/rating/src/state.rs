//! Shared application state — the axum equivalent of `app.state.*`.

use std::sync::Arc;

use bss_clients::CatalogClient;
use bss_db::PgPool;
use bss_events::MqChannel;

use crate::config::Settings;

/// Injected into every rating route handler via `State<AppState>`. Cheap to
/// clone (pool + reqwest client are `Arc`-backed; `MqChannel` is shared).
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub catalog: CatalogClient,
    pub settings: Settings,
    /// Present only when `BSS_MQ_URL` is configured — the inline-publish handle
    /// the consumer uses. `None` mirrors the Python "mq.not_configured" path.
    pub mq: Option<Arc<MqChannel>>,
}
