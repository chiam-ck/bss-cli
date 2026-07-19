//! Shared application state — the axum equivalent of `app.state.*`.

use bss_clients::LoyaltyClient;
use bss_db::PgPool;

use crate::config::Settings;

/// Injected into every catalog route handler via `State<AppState>`. Cheap to
/// clone (pool + reqwest client are `Arc`-backed).
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    /// Present only when `BSS_LOYALTY_API_TOKEN` is set — the promo subsystem is
    /// OFF (`None`) otherwise, matching the Python graceful-degrade path.
    pub loyalty: Option<LoyaltyClient>,
    pub settings: Settings,
}
