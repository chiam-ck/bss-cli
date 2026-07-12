//! Shared application state.

use bss_clients::InventoryClient;
use bss_db::PgPool;

use crate::config::Settings;

/// Injected into every SOM route handler. The consumers + relay hold their own
/// clones of the pool / inventory client / MqChannel (built in `main`).
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub inventory: InventoryClient,
    pub settings: Settings,
}
