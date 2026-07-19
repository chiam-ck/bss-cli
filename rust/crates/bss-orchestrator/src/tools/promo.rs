//! Promotion tools — TMF671. Port of `orchestrator/bss_orchestrator/tools/promo.py`.
//! `promo.show` is a verbatim read; the **write** tools
//! (`register_promo_write_tools`: `create`/`assign`) drive the create-promotion saga.

use std::sync::Arc;

use bss_clients::CatalogClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_SHOW: &str = include_str!("desc/promo_show.txt");
const DESC_CREATE: &str = include_str!("desc/promo_create.txt");
const DESC_ASSIGN: &str = include_str!("desc/promo_assign.txt");

/// Optional list-of-strings arg (e.g. `applicable_offering_ids` / `customer_ids`).
fn opt_str_list(args: &Value, key: &str) -> Option<Vec<String>> {
    args.get(key).and_then(Value::as_array).map(|a| {
        a.iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect()
    })
}

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

/// Register the two promo **write** tools, each capturing a clone of `client`.
pub fn register_promo_write_tools(registry: &mut ToolRegistry, client: CatalogClient) {
    // promo.create — the create-promotion saga. audience defaults "public",
    // currency "SGD"; the rest are optional pass-throughs.
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "promo.create".to_string(),
        description: DESC_CREATE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let promotion_id = req_str(&args, "promotion_id")?;
                let discount_type = req_str(&args, "discount_type")?;
                let discount_value = req_str(&args, "discount_value")?;
                let duration_kind = req_str(&args, "duration_kind")?;
                let audience = opt_str(&args, "audience").unwrap_or_else(|| "public".to_string());
                let currency = opt_str(&args, "currency").unwrap_or_else(|| "SGD".to_string());
                let code = opt_str(&args, "code");
                let promo_code_kind = opt_str(&args, "promo_code_kind");
                let applicable = opt_str_list(&args, "applicable_offering_ids");
                let periods_total = args.get("periods_total").and_then(Value::as_i64);
                let valid_from = opt_str(&args, "valid_from");
                let valid_to = opt_str(&args, "valid_to");
                let display_name = opt_str(&args, "display_name");
                c.create_promotion(
                    &promotion_id,
                    &discount_type,
                    &discount_value,
                    &duration_kind,
                    &audience,
                    &currency,
                    code.as_deref(),
                    promo_code_kind.as_deref(),
                    applicable.as_deref(),
                    periods_total,
                    valid_from.as_deref(),
                    valid_to.as_deref(),
                    display_name.as_deref(),
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "promo.assign".to_string(),
        description: DESC_ASSIGN.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let promotion_id = req_str(&args, "promotion_id")?;
                let customer_ids = opt_str_list(&args, "customer_ids").unwrap_or_default();
                c.assign_promotion(&promotion_id, &customer_ids)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });
}
