//! Shared application state — the axum equivalent of `app.state.*`.

use bss_clients::{
    CatalogClient, CrmClient, LoyaltyClient, PaymentClient, SomClient, SubscriptionClient,
};
use bss_db::PgPool;

use crate::config::Settings;

/// Injected into every com route handler via `State<AppState>`. Cheap to clone
/// (pool + reqwest clients are `Arc`-backed).
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub crm: CrmClient,
    pub catalog: CatalogClient,
    pub payment: PaymentClient,
    pub som: SomClient,
    pub subscription: SubscriptionClient,
    /// `None` when `BSS_LOYALTY_API_TOKEN` is unset (promo consume OFF).
    pub loyalty: Option<LoyaltyClient>,
    pub settings: Settings,
}
