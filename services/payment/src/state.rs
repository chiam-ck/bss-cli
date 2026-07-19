//! Shared application state — the axum equivalent of `app.state.*`.

use bss_clients::CrmClient;
use bss_db::PgPool;

use crate::config::Settings;
use crate::tokenizer::Tokenizer;

/// Injected into every route handler via `State<AppState>`. Cheap to clone
/// (pool + reqwest client are `Arc`-backed; `Tokenizer` holds an `Arc` client).
#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub crm: CrmClient,
    /// Resolved once at startup by `select_tokenizer` (mock | stripe).
    pub tokenizer: Tokenizer,
    pub settings: Settings,
}
