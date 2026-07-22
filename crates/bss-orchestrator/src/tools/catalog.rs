//! Catalog read tools — TMF620 product offerings + VAS. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/catalog.py`.
//!
//! Each tool is a thin wrapper: it returns the `CatalogClient` response verbatim,
//! so byte-parity of the tool output follows transitively from the P3 catalog
//! service golden diff (Rust catalog == Python catalog). This is the template for
//! the remaining client-backed tool families — a closure capturing its typed
//! client, mapping `ClientError` to the structured tool observation.
//!
//! The admin **write** tools (`add_offering`/`add_price`/`window_offering`/
//! `retire_offering` — `register_catalog_admin_write_tools`) live here too. As of
//! v2.1 they are on the operator's LLM surface, gated by propose-then-`/confirm`
//! ([`crate::safety::DESTRUCTIVE_TOOLS`]) rather than hidden. Their descriptions
//! carry an *elicitation contract* — required vs. optional fields, and an explicit
//! "ask, never assume" — because the OpenAI function schema this orchestrator emits
//! is permissive (`additionalProperties: true`), so the prose IS the arg contract.
//! [`super::require_fields`] backs the prose with a server-side gate.

use std::sync::Arc;

use bss_clients::CatalogClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{
    map_client_err as map_err, opt_str, req_str, require_fields, RegisteredTool, ToolError,
    ToolRegistry,
};

const DESC_LIST_OFFERINGS: &str = include_str!("desc/catalog_list_offerings.txt");
const DESC_GET_OFFERING: &str = include_str!("desc/catalog_get_offering.txt");
const DESC_LIST_VAS: &str = include_str!("desc/catalog_list_vas.txt");
const DESC_GET_VAS: &str = include_str!("desc/catalog_get_vas.txt");
const DESC_LIST_ACTIVE_OFFERINGS: &str = include_str!("desc/catalog_list_active_offerings.txt");
const DESC_GET_ACTIVE_PRICE: &str = include_str!("desc/catalog_get_active_price.txt");
const DESC_ADD_OFFERING: &str = include_str!("desc/catalog_add_offering.txt");
const DESC_ADD_PRICE: &str = include_str!("desc/catalog_add_price.txt");
const DESC_WINDOW_OFFERING: &str = include_str!("desc/catalog_window_offering.txt");
const DESC_RETIRE_OFFERING: &str = include_str!("desc/catalog_retire_offering.txt");

/// The allowance fields on an offering. `add_offering` requires at least one to be
/// present: a bundled-prepaid plan that grants nothing is never what the operator
/// meant, and silently creating one hides the omission until a customer buys it.
const ALLOWANCE_FIELDS: &[&str] = &["data_mb", "voice_minutes", "sms_count", "data_roaming_mb"];

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

/// Register the four catalog **admin write** tools, each capturing a clone of
/// `client`. `valid_from`/`valid_to` are ISO strings passed verbatim.
pub fn register_catalog_admin_write_tools(registry: &mut ToolRegistry, client: CatalogClient) {
    // catalog.add_offering — currency defaults SGD; spec is SPEC_MOBILE_PREPAID
    // (the client default); window optional. Identity, price, and at least one
    // allowance must come from the operator.
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "catalog.add_offering".to_string(),
        description: DESC_ADD_OFFERING.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                require_fields(&args, &["offering_id", "name", "amount"])?;
                if !ALLOWANCE_FIELDS.iter().any(|k| args.get(*k).is_some()) {
                    return Err(ToolError::Other {
                        kind: crate::tools::MISSING_REQUIRED_FIELDS.to_string(),
                        detail: "an offering needs at least one allowance — ask the \
                                 operator how much data_mb / voice_minutes / sms_count / \
                                 data_roaming_mb this plan includes, then call again. \
                                 Pass 0 for an allowance the plan deliberately omits; do \
                                 NOT pick the numbers yourself."
                            .to_string(),
                    });
                }
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
                require_fields(&args, &["offering_id", "price_id", "amount"])?;
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

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "catalog.window_offering".to_string(),
        description: DESC_WINDOW_OFFERING.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                require_fields(&args, &["offering_id"])?;
                // Both bounds absent would clear the window rather than set one —
                // almost never the intent, and indistinguishable from the model
                // simply not having asked. An explicit `null` IS a clear, so this
                // checks key presence, not value.
                if args.get("valid_from").is_none() && args.get("valid_to").is_none() {
                    return Err(ToolError::Other {
                        kind: crate::tools::MISSING_REQUIRED_FIELDS.to_string(),
                        detail: "a window needs at least one bound — ask the operator \
                                 for valid_from and/or valid_to (ISO-8601, from \
                                 `clock.now`), then call again. Pass an explicit null \
                                 for a bound the operator wants CLEARED; omitting both \
                                 is not a way to say 'leave it alone'."
                            .to_string(),
                    });
                }
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

    // catalog.retire_offering — the `D` of the CRUD set. No hard delete exists:
    // retiring stamps `lifecycle_status=retired` + `is_sellable=false` and closes
    // any open window, leaving existing subscriptions on their price snapshot.
    let c = client;
    registry.register(RegisteredTool {
        name: "catalog.retire_offering".to_string(),
        description: DESC_RETIRE_OFFERING.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                require_fields(&args, &["offering_id"])?;
                let offering_id = req_str(&args, "offering_id")?;
                c.admin_retire_offering(&offering_id).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    use bss_clients::TokenAuthProvider;
    use serde_json::json;

    use crate::tools::ToolCtx;

    /// A registry with only the catalog admin writes. The client points nowhere —
    /// every assertion below trips the elicitation gate BEFORE any network call,
    /// which is itself the point: an under-specified call must never reach the
    /// service.
    fn admin_registry() -> ToolRegistry {
        let auth = Arc::new(TokenAuthProvider::new("x").unwrap());
        let client = CatalogClient::new("http://127.0.0.1:1", auth).unwrap();
        let mut r = ToolRegistry::new();
        register_catalog_admin_write_tools(&mut r, client);
        r
    }

    /// Call a tool and return the observation string it produced. Panics if the
    /// call unexpectedly succeeded (it cannot — the client points at a dead port).
    async fn call_err(name: &str, args: Value) -> String {
        let reg = admin_registry();
        let tool = reg.get(name).expect("registered");
        match (tool.func)(args, ToolCtx::default()).await {
            Err(e) => e.to_observation(),
            Ok(v) => panic!("expected an error, got {v}"),
        }
    }

    #[tokio::test]
    async fn add_offering_names_every_missing_field_at_once() {
        // One question for the operator, not three round-trips.
        let obs = call_err("catalog.add_offering", json!({})).await;
        assert!(obs.contains("MISSING_REQUIRED_FIELDS"), "{obs}");
        assert!(obs.contains("'offering_id'"), "{obs}");
        assert!(obs.contains("'name'"), "{obs}");
        assert!(obs.contains("'amount'"), "{obs}");
        assert!(obs.contains("Do NOT invent"), "{obs}");
    }

    #[tokio::test]
    async fn add_offering_rejects_a_plan_that_grants_nothing() {
        let obs = call_err(
            "catalog.add_offering",
            json!({"offering_id": "PLAN_XS", "name": "Lite", "amount": "9.00"}),
        )
        .await;
        assert!(obs.contains("MISSING_REQUIRED_FIELDS"), "{obs}");
        assert!(obs.contains("at least one allowance"), "{obs}");
        // An explicit zero IS an answer — it must clear the gate (and then fail on
        // transport, proving the gate let it through).
        let obs = call_err(
            "catalog.add_offering",
            json!({
                "offering_id": "PLAN_XS", "name": "Lite", "amount": "9.00",
                "data_roaming_mb": 0
            }),
        )
        .await;
        assert!(!obs.contains("MISSING_REQUIRED_FIELDS"), "{obs}");
    }

    #[tokio::test]
    async fn add_price_requires_the_row_identity_and_amount() {
        let obs = call_err("catalog.add_price", json!({"offering_id": "PLAN_M"})).await;
        assert!(obs.contains("'price_id'"), "{obs}");
        assert!(obs.contains("'amount'"), "{obs}");
        assert!(!obs.contains("'offering_id'"), "{obs}");
    }

    #[tokio::test]
    async fn window_offering_needs_a_bound_but_honours_an_explicit_null() {
        // Neither bound → the model never asked. Rejected.
        let obs = call_err("catalog.window_offering", json!({"offering_id": "PLAN_M"})).await;
        assert!(obs.contains("MISSING_REQUIRED_FIELDS"), "{obs}");
        assert!(obs.contains("at least one bound"), "{obs}");
        // An explicit null is a deliberate CLEAR, not an omission — it passes.
        let obs = call_err(
            "catalog.window_offering",
            json!({"offering_id": "PLAN_M", "valid_to": null}),
        )
        .await;
        assert!(!obs.contains("MISSING_REQUIRED_FIELDS"), "{obs}");
    }

    #[tokio::test]
    async fn retire_offering_requires_an_id() {
        let obs = call_err("catalog.retire_offering", json!({})).await;
        assert!(obs.contains("MISSING_REQUIRED_FIELDS"), "{obs}");
        assert!(obs.contains("'offering_id'"), "{obs}");
    }

    /// An empty string is the shape a model reaches for when it wants to "leave a
    /// field blank" — it must read as absent, not as a valid id.
    #[tokio::test]
    async fn blank_strings_count_as_missing() {
        let obs = call_err("catalog.retire_offering", json!({"offering_id": ""})).await;
        assert!(obs.contains("MISSING_REQUIRED_FIELDS"), "{obs}");
    }
}
