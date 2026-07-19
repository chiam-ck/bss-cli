//! Shared application state — the axum equivalent of `app.state.*`.

use bss_clients::{LoyaltyClient, SubscriptionClient};
use bss_db::PgPool;

use crate::config::Settings;

/// Injected into every crm route handler via `State<AppState>`. Cheap to clone
/// (pool + reqwest clients are `Arc`-backed).
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub subscription: SubscriptionClient,
    /// `None` when `BSS_LOYALTY_API_TOKEN` is unset (customer-registry sync OFF).
    pub loyalty: Option<LoyaltyClient>,
    pub settings: Settings,
}
