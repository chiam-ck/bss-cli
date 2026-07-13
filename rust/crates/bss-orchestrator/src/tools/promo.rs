//! Promotion read tool — TMF671. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/promo.py`. Verbatim `CatalogClient`
//! wrapper. The promo writes (`create`/`assign`) land with the catalog/loyalty write
//! slice.

use std::sync::Arc;

use bss_clients::CatalogClient;
use futures_util::future::FutureExt;

use super::{map_client_err as map_err, req_str, RegisteredTool, ToolRegistry};

const DESC_SHOW: &str = include_str!("desc/promo_show.txt");

/// Register the `promo.show` read tool, capturing a clone of `client`.
pub fn register_promo_tools(registry: &mut ToolRegistry, client: CatalogClient) {
    let c = client;
    registry.register(RegisteredTool {
        name: "promo.show".to_string(),
        description: DESC_SHOW.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "promotion_id")?;
                c.get_promotion(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}
