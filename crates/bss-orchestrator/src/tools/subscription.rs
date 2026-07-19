//! Subscription read tools — the go-to diagnostic reads. Port of the read slice of
//! `orchestrator/bss_orchestrator/tools/subscription.py`.
//!
//! Every tool returns the `SubscriptionClient` response verbatim, so byte-parity
//! follows transitively from the P4 subscription service golden diff. Note
//! `subscription.get_esim_activation` is a **projected-dict** convenience — the
//! client reads the subscription and projects five fixed keys; its wire order
//! matches Python's dict-literal order via D9's `serde_json` `preserve_order`.
//!
//! The subscription **write** tools (`register_subscription_write_tools`) live here
//! too; the ownership-bound `*.mine` chat wrappers land in a later slice.

use std::sync::Arc;

use bss_clients::SubscriptionClient;
use futures_util::future::FutureExt;
use serde_json::Value;

use super::{map_client_err as map_err, opt_str, req_str, RegisteredTool, ToolRegistry};

const DESC_GET: &str = include_str!("desc/subscription_get.txt");
const DESC_LIST_FOR_CUSTOMER: &str = include_str!("desc/subscription_list_for_customer.txt");
const DESC_GET_BALANCE: &str = include_str!("desc/subscription_get_balance.txt");
const DESC_GET_ESIM_ACTIVATION: &str = include_str!("desc/subscription_get_esim_activation.txt");
const DESC_TERMINATE: &str = include_str!("desc/subscription_terminate.txt");
const DESC_PURCHASE_VAS: &str = include_str!("desc/subscription_purchase_vas.txt");
const DESC_RENEW_NOW: &str = include_str!("desc/subscription_renew_now.txt");
const DESC_TICK_RENEWALS: &str = include_str!("desc/subscription_tick_renewals_now.txt");
const DESC_SCHEDULE_PLAN_CHANGE: &str = include_str!("desc/subscription_schedule_plan_change.txt");
const DESC_CANCEL_PLAN_CHANGE: &str =
    include_str!("desc/subscription_cancel_pending_plan_change.txt");
const DESC_MIGRATE_PRICE: &str = include_str!("desc/subscription_migrate_to_new_price.txt");

/// Register the four subscription **read** tools, each capturing a clone of `client`.
pub fn register_subscription_tools(registry: &mut ToolRegistry, client: SubscriptionClient) {
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.get".to_string(),
        description: DESC_GET.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                c.get(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.list_for_customer".to_string(),
        description: DESC_LIST_FOR_CUSTOMER.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let cid = req_str(&args, "customer_id")?;
                c.list_for_customer(&cid).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.get_balance".to_string(),
        description: DESC_GET_BALANCE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                c.get_balance(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client;
    registry.register(RegisteredTool {
        name: "subscription.get_esim_activation".to_string(),
        description: DESC_GET_ESIM_ACTIVATION.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                c.get_esim_activation(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });
}

/// Register the seven subscription **write** tools, each capturing a clone of
/// `client`. `subscription.terminate` is destructive; `subscription.migrate_to_new_
/// price` is LLM-hidden (operator/scenario only). Safety gating lives in the loop.
pub fn register_subscription_write_tools(registry: &mut ToolRegistry, client: SubscriptionClient) {
    // subscription.terminate — operator terminate: no reason, release inventory.
    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.terminate".to_string(),
        description: DESC_TERMINATE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                c.terminate_with_reason(&id, None, true)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.purchase_vas".to_string(),
        description: DESC_PURCHASE_VAS.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                let vas = req_str(&args, "vas_offering_id")?;
                c.purchase_vas(&id, &vas).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.renew_now".to_string(),
        description: DESC_RENEW_NOW.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                c.renew(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.tick_renewals_now".to_string(),
        description: DESC_TICK_RENEWALS.to_string(),
        func: Arc::new(move |_args, _ctx| {
            let c = c.clone();
            async move { c.tick_renewals_now().await.map_err(map_err) }.boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.schedule_plan_change".to_string(),
        description: DESC_SCHEDULE_PLAN_CHANGE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                let new_offering = req_str(&args, "new_offering_id")?;
                c.schedule_plan_change(&id, &new_offering)
                    .await
                    .map_err(map_err)
            }
            .boxed()
        }),
    });

    let c = client.clone();
    registry.register(RegisteredTool {
        name: "subscription.cancel_pending_plan_change".to_string(),
        description: DESC_CANCEL_PLAN_CHANGE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let id = req_str(&args, "subscription_id")?;
                c.cancel_plan_change(&id).await.map_err(map_err)
            }
            .boxed()
        }),
    });

    // subscription.migrate_to_new_price — LLM-hidden admin. notice_days defaults 30,
    // initiated_by defaults "ops"; effective_from is the caller's ISO instant (sent
    // verbatim — the Python fromisoformat→isoformat round-trip is a no-op for a
    // canonical datetime).
    let c = client;
    registry.register(RegisteredTool {
        name: "subscription.migrate_to_new_price".to_string(),
        description: DESC_MIGRATE_PRICE.to_string(),
        func: Arc::new(move |args, _ctx| {
            let c = c.clone();
            async move {
                let offering_id = req_str(&args, "offering_id")?;
                let new_price_id = req_str(&args, "new_price_id")?;
                let effective_from = req_str(&args, "effective_from")?;
                let notice_days = args
                    .get("notice_days")
                    .and_then(Value::as_i64)
                    .unwrap_or(30);
                let initiated_by =
                    opt_str(&args, "initiated_by").unwrap_or_else(|| "ops".to_string());
                c.migrate_to_new_price(
                    &offering_id,
                    &new_price_id,
                    &effective_from,
                    notice_days,
                    &initiated_by,
                )
                .await
                .map_err(map_err)
            }
            .boxed()
        }),
    });
}
