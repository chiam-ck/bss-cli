//! Catalog read tools — TMF620 product offerings + VAS. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/catalog.py`.
//!
//! Each tool is a thin wrapper: it returns the `CatalogClient` response verbatim,
//! so byte-parity of the tool output follows transitively from the P3 catalog
//! service golden diff (Rust catalog == Python catalog). This is the template for
//! the remaining client-backed tool families — a closure capturing its typed
//! client, mapping `ClientError` to the structured tool observation.
//!
//! The admin **write** tools (`add_offering`/`add_price`/`window_offering`, hidden
//! from the LLM — `register_catalog_admin_write_tools`) live here too.

use std::sync::Arc;

use bss_clients::CatalogClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_LIST_OFFERINGS: &str = include_str!("desc/catalog_list_offerings.txt");
const DESC_GET_OFFERING: &str = include_str!("desc/catalog_get_offering.txt");
const DESC_LIST_VAS: &str = include_str!("desc/catalog_list_vas.txt");
const DESC_GET_VAS: &str = include_str!("desc/catalog_get_vas.txt");
const DESC_LIST_ACTIVE_OFFERINGS: &str = include_str!("desc/catalog_list_active_offerings.txt");
const DESC_GET_ACTIVE_PRICE: &str = include_str!("desc/catalog_get_active_price.txt");
const DESC_ADD_OFFERING: &str = include_str!("desc/catalog_add_offering.txt");
const DESC_ADD_PRICE: &str = include_str!("desc/catalog_add_price.txt");
const DESC_WINDOW_OFFERING: &str = include_str!("desc/catalog_window_offering.txt");

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

/// Register the three catalog **admin write** tools (all LLM-hidden), each capturing
/// a clone of `client`. `valid_from`/`valid_to` are ISO strings passed verbatim.
pub fn register_catalog_admin_write_tools(registry: &mut ToolRegistry, client: CatalogClient) {
    // catalog.add_offering — currency defaults SGD; spec is SPEC_MOBILE_PREPAID
    // (the client default); allowances/window optional.
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "catalog.add_offering".to_string(),
        description: DESC_ADD_OFFERING.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let offering_id = req_str(&args, "offering_id")?;
                let name = req_str(&args, "name")?;
                let amount = req_str(&args, "amount")?;
                let currency = opt_str(&args, "currency").unwrap_or_else(|| "SGD".to_string());
                let valid_from = opt_str(&args, "valid_from");
                let valid_to = opt_str(&args, "valid_to");
                let data_mb = args.get("data_mb").and_then(Value::as_i64);
                let voice_minutes = args.get("voice_minutes").and_then(Value::as_i64);
                let sms_count = args.get("sms_count").and_then(Value::as_i64);
                let data_roaming_mb = args.get("data_roaming_mb").and_then(Value::as_i64);
                c.admin_add_offering(
                    &offering_id,
                    &name,
                    &amount,
                    &currency,
                    "SPEC_MOBILE_PREPAID",
                    valid_from.as_deref(),
                    valid_to.as_deref(),
                    data_mb,
                    voice_minutes,
                    sms_count,
                    data_roaming_mb,
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "catalog.add_price".to_string(),
        description: DESC_ADD_PRICE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let offering_id = req_str(&args, "offering_id")?;
                let price_id = req_str(&args, "price_id")?;
                let amount = req_str(&args, "amount")?;
                let currency = opt_str(&args, "currency").unwrap_or_else(|| "SGD".to_string());
                let valid_from = opt_str(&args, "valid_from");
                let valid_to = opt_str(&args, "valid_to");
                let retire_current = args
                    .get("retire_current")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                c.admin_add_price(
                    &offering_id,
                    &price_id,
                    &amount,
                    &currency,
                    valid_from.as_deref(),
                    valid_to.as_deref(),
                    retire_current,
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "catalog.window_offering".to_string(),
        description: DESC_WINDOW_OFFERING.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let offering_id = req_str(&args, "offering_id")?;
                let valid_from = opt_str(&args, "valid_from");
                let valid_to = opt_str(&args, "valid_to");
                c.admin_set_offering_window(
                    &offering_id,
                    valid_from.as_deref(),
                    valid_to.as_deref(),
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });
}
