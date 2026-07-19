//! Shared application state — the axum equivalent of `app.state.*`.

use bss_clients::{CatalogClient, CrmClient, InventoryClient, PaymentClient};
use bss_db::PgPool;

use crate::config::Settings;

/// Injected into every subscription route handler via `State<AppState>`. Cheap to
/// clone (pool + reqwest clients are `Arc`-backed).
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub crm: CrmClient,
    pub payment: PaymentClient,
    pub catalog: CatalogClient,
    pub inventory: InventoryClient,
    pub settings: Settings,
}
