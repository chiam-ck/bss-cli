//! Catalog read tools — TMF620 product offerings + VAS. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/catalog.py`.
//!
//! Each tool is a thin wrapper: it returns the `CatalogClient` response verbatim,
//! so byte-parity of the tool output follows transitively from the P3 catalog
//! service golden diff (Rust catalog == Python catalog). This is the template for
//! the remaining client-backed tool families — a closure capturing its typed
//! client, mapping `ClientError` to the structured tool observation.
//!
//! The admin write tools (`add_offering`/`add_price`/`window_offering`, hidden
//! from the LLM) land with the admin client methods in a later slice.

use std::sync::Arc;

use bss_clients::CatalogClient;
use futures_util::future::FutureExt;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_LIST_OFFERINGS: &str = include_str!("desc/catalog_list_offerings.txt");
const DESC_GET_OFFERING: &str = include_str!("desc/catalog_get_offering.txt");
const DESC_LIST_VAS: &str = include_str!("desc/catalog_list_vas.txt");
const DESC_GET_VAS: &str = include_str!("desc/catalog_get_vas.txt");
const DESC_LIST_ACTIVE_OFFERINGS: &str = include_str!("desc/catalog_list_active_offerings.txt");
const DESC_GET_ACTIVE_PRICE: &str = include_str!("desc/catalog_get_active_price.txt");

/// Register the six catalog **read** tools, each capturing a clone of `client`.
pub fn register_catalog_tools(registry: &mut ToolRegistry, client: CatalogClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "catalog.list_offerings".to_string(),
        description: DESC_LIST_OFFERINGS.to_string(),
        func: Arc::new(move |_args, _ctx| {
            let c = c.clone();
            async move { c.list_offerings().await.map_err(map_err) }.boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "catalog.get_offering".to_string(),
        description: DESC_GET_OFFERING.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "offering_id")?;
                c.get_offering(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "catalog.list_vas".to_string(),
        description: DESC_LIST_VAS.to_string(),
        func: Arc::new(move |_args, _ctx| {
            let c = c.clone();
            async move { c.list_vas().await.map_err(map_err) }.boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "catalog.get_vas".to_string(),
        description: DESC_GET_VAS.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "vas_offering_id")?;
                c.get_vas(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // Defaults `at` to now (matching the Python client's `clock_now()` default).
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "catalog.list_active_offerings".to_string(),
        description: DESC_LIST_ACTIVE_OFFERINGS.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let at =
                    opt_str(&args, "at").unwrap_or_else(|| bss_clock::isoformat(bss_clock::now()));
                c.list_active_offerings(&at).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "catalog.get_active_price".to_string(),
        description: DESC_GET_ACTIVE_PRICE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "offering_id")?;
                let at = opt_str(&args, "at");
                c.get_active_price_at(&id, at.as_deref())
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });
}
