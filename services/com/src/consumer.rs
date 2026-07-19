//! COM consumers — port of `app.events.consumer`.
//!
//! Two queues on the safe (retry/park + inbox-dedup) consumer:
//! `service_order.completed` → activate (subscription + promo consume);
//! `service_order.failed` → mark the order failed. Handlers run with
//! `RequestCtx::default` and never publish inline — they stage events for the
//! outbox relay (bind_consumer owns the commit).

use std::sync::Arc;

use bss_clients::{LoyaltyClient, SubscriptionClient};
use bss_context::RequestCtx;
use bss_db::PgPool;
use bss_events::{bind_consumer, EventHandler, MqChannel};
use serde_json::Value;

use crate::service::{
    handle_service_order_completed, handle_service_order_failed, ServiceOrderCompleted,
};

const INBOX_SCHEMA: &str = "order_mgmt";

/// Spawn a background consumer task per queue. Best-effort: a bind failure on one
/// queue is logged and doesn't stop the other.
pub fn spawn_consumers(
    mq: Arc<MqChannel>,
    pool: PgPool,
    subscription: SubscriptionClient,
    loyalty: Option<LoyaltyClient>,
    max_retries: u32,
    retry_backoff_ms: u64,
) {
    let queues: Vec<(&'static str, &'static str, EventHandler)> = vec![
        (
            "com.service_order.completed",
            "service_order.completed",
            completed_handler(subscription.clone(), loyalty.clone()),
        ),
        (
            "com.service_order.failed",
            "service_order.failed",
            failed_handler(),
        ),
    ];

    for (queue_name, routing_key, handler) in queues {
        let mq = mq.clone();
        let pool = pool.clone();
        tokio::spawn(async move {
            if let Err(e) = bind_consumer(
                mq,
                pool,
                queue_name,
                routing_key,
                // Unique consumer tag per queue (the SOM P2 lesson).
                queue_name,
                INBOX_SCHEMA,
                max_retries,
                retry_backoff_ms,
                handler,
            )
            .await
            {
                tracing::error!(error = %e, queue = queue_name, "mq.consumer.stopped");
            }
        });
    }
}

fn completed_handler(
    subscription: SubscriptionClient,
    loyalty: Option<LoyaltyClient>,
) -> EventHandler {
    Arc::new(move |conn, body| {
        let subscription = subscription.clone();
        let loyalty = loyalty.clone();
        Box::pin(async move {
            let ctx = RequestCtx::default();
            let p = ServiceOrderCompleted {
                commercial_order_id: req_str(&body, "commercialOrderId")?,
                customer_id: req_str(&body, "customerId")?,
                offering_id: req_str(&body, "offeringId")?,
                msisdn: req_str(&body, "msisdn")?,
                iccid: req_str(&body, "iccid")?,
                payment_method_id: req_str(&body, "paymentMethodId")?,
                cfs_service_id: opt_str(&body, "cfsServiceId").to_string(),
                price_snapshot: body.get("priceSnapshot").cloned().filter(|v| !v.is_null()),
            };
            handle_service_order_completed(conn, &subscription, loyalty.as_ref(), &ctx, p)
                .await
                .map_err(|e| format!("{e:?}"))
        })
    })
}

fn failed_handler() -> EventHandler {
    Arc::new(move |conn, body| {
        Box::pin(async move {
            let ctx = RequestCtx::default();
            handle_service_order_failed(
                conn,
                &ctx,
                &req_str(&body, "commercialOrderId")?,
                opt_str(&body, "reason"),
            )
            .await
            .map_err(|e| format!("{e:?}"))
        })
    })
}

/// A required body field — a missing one fails the handler (retry/park), matching
/// the Python `body["key"]` KeyError.
fn req_str(body: &Value, key: &str) -> Result<String, String> {
    body.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| format!("missing required field '{key}'"))
}

fn opt_str<'a>(body: &'a Value, key: &str) -> &'a str {
    body.get(key).and_then(Value::as_str).unwrap_or("")
}
