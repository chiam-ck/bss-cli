//! SOM consumers — port of `app.events.consumer`.
//!
//! Four queues, each on the safe (retry/park + inbox-dedup) consumer:
//! `order.in_progress` → decompose; `provisioning.task.{completed,failed,stuck}`
//! → the SOM handlers. Handlers run with `RequestCtx::default` (the consumer has
//! no request context — Python `auth_context.current()` default) and never
//! publish inline; they stage events for the outbox relay.

use std::sync::Arc;

use bss_clients::InventoryClient;
use bss_context::RequestCtx;
use bss_db::PgPool;
use bss_events::{bind_consumer, EventHandler, MqChannel};
use serde_json::Value;

use crate::decomposition::{decompose_order, DecomposeRequest};
use crate::service::{handle_task_completed, handle_task_failed, handle_task_stuck};

const INBOX_SCHEMA: &str = "service_inventory";

/// Spawn a background consumer task per queue. Best-effort: a bind failure on one
/// queue is logged and doesn't stop the others.
pub fn spawn_consumers(
    mq: Arc<MqChannel>,
    pool: PgPool,
    inventory: InventoryClient,
    max_retries: u32,
    retry_backoff_ms: u64,
) {
    let queues: Vec<(&'static str, &'static str, EventHandler)> = vec![
        (
            "som.order.in_progress",
            "order.in_progress",
            decompose_handler(inventory.clone()),
        ),
        (
            "som.provisioning.task.completed",
            "provisioning.task.completed",
            task_completed_handler(),
        ),
        (
            "som.provisioning.task.failed",
            "provisioning.task.failed",
            task_failed_handler(inventory.clone()),
        ),
        (
            "som.provisioning.task.stuck",
            "provisioning.task.stuck",
            task_stuck_handler(),
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
                // Unique consumer tag per queue — RabbitMQ rejects a reused tag on
                // one connection (the queue name is already unique).
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

fn decompose_handler(inventory: InventoryClient) -> EventHandler {
    Arc::new(move |conn, body| {
        let inventory = inventory.clone();
        Box::pin(async move {
            let ctx = RequestCtx::default();
            let req = DecomposeRequest {
                commercial_order_id: req_str(&body, "commercialOrderId")?,
                customer_id: req_str(&body, "customerId")?,
                offering_id: req_str(&body, "offeringId")?,
                msisdn_preference: body
                    .get("msisdnPreference")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                payment_method_id: req_str(&body, "paymentMethodId")?,
                price_snapshot: body.get("priceSnapshot").cloned().filter(|v| !v.is_null()),
            };
            decompose_order(conn, &inventory, &ctx, &req).await
        })
    })
}

fn task_completed_handler() -> EventHandler {
    Arc::new(move |conn, body| {
        Box::pin(async move {
            let ctx = RequestCtx::default();
            handle_task_completed(
                conn,
                &ctx,
                &req_str(&body, "serviceId")?,
                &req_str(&body, "taskType")?,
                &req_str(&body, "serviceOrderId")?,
                opt_str(&body, "commercialOrderId"),
            )
            .await
        })
    })
}

fn task_failed_handler(inventory: InventoryClient) -> EventHandler {
    Arc::new(move |conn, body| {
        let inventory = inventory.clone();
        Box::pin(async move {
            let ctx = RequestCtx::default();
            let permanent = body
                .get("permanent")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            handle_task_failed(
                conn,
                &inventory,
                &ctx,
                &req_str(&body, "serviceId")?,
                &req_str(&body, "taskType")?,
                &req_str(&body, "serviceOrderId")?,
                opt_str(&body, "commercialOrderId"),
                permanent,
            )
            .await
        })
    })
}

fn task_stuck_handler() -> EventHandler {
    Arc::new(move |conn, body| {
        Box::pin(async move {
            handle_task_stuck(
                conn,
                &req_str(&body, "serviceId")?,
                &req_str(&body, "taskType")?,
                &req_str(&body, "serviceOrderId")?,
            )
            .await
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

/// An optional body field, defaulting to `""` (Python `body.get(key, "")`).
fn opt_str<'a>(body: &'a Value, key: &str) -> &'a str {
    body.get(key).and_then(Value::as_str).unwrap_or("")
}
