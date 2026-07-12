//! Subscription consumer — port of `app.events.consumer`.
//!
//! One queue on the safe (retry/park + inbox-dedup) consumer: `usage.rated` →
//! `handle_usage_rated` (the block-on-exhaust decrement). The handler runs with
//! `RequestCtx::default` and never publishes inline — it stages events for the
//! outbox relay (bind_consumer owns the commit + the `FOR UPDATE` serialization).
//!
//! The Python service also ran a `notification.requested` stdout logger (a dev
//! inbox). It has no API/DB effect, so it is intentionally not ported (matching
//! com's decision) — the durable `audit.domain_event` row is the substrate.

use std::sync::Arc;

use bss_context::RequestCtx;
use bss_db::PgPool;
use bss_events::{bind_consumer, EventHandler, MqChannel};
use serde_json::Value;

use crate::service::handle_usage_rated;

const INBOX_SCHEMA: &str = "subscription";

pub fn spawn_consumers(mq: Arc<MqChannel>, pool: PgPool, max_retries: u32, retry_backoff_ms: u64) {
    let queue_name = "subscription.usage.rated";
    let routing_key = "usage.rated";
    let mq2 = mq.clone();
    tokio::spawn(async move {
        if let Err(e) = bind_consumer(
            mq2,
            pool,
            queue_name,
            routing_key,
            // Unique consumer tag per queue (the SOM P2 lesson).
            queue_name,
            INBOX_SCHEMA,
            max_retries,
            retry_backoff_ms,
            usage_rated_handler(),
        )
        .await
        {
            tracing::error!(error = %e, queue = queue_name, "mq.consumer.stopped");
        }
    });
}

fn usage_rated_handler() -> EventHandler {
    Arc::new(move |conn, body| {
        Box::pin(async move {
            let ctx = RequestCtx::default();
            let subscription_id = req_str(&body, "subscriptionId")?;
            let allowance_type = req_str(&body, "allowanceType")?;
            let consumed_quantity = req_int(&body, "consumedQuantity")?;
            let usage_event_id = opt_str(&body, "usageEventId").to_string();
            handle_usage_rated(
                conn,
                &ctx,
                &subscription_id,
                &allowance_type,
                consumed_quantity,
                &usage_event_id,
            )
            .await
            .map_err(|e| format!("{e:?}"))
        })
    })
}

/// A required string body field — a missing one fails the handler (retry/park),
/// matching the Python `body["key"]` KeyError.
fn req_str(body: &Value, key: &str) -> Result<String, String> {
    body.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| format!("missing required field '{key}'"))
}

/// `int(body["consumedQuantity"])` — accepts a JSON number or a numeric string.
fn req_int(body: &Value, key: &str) -> Result<i64, String> {
    match body.get(key) {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .ok_or_else(|| format!("field '{key}' not an integer")),
        Some(Value::String(s)) => s
            .parse::<i64>()
            .map_err(|_| format!("field '{key}' not an integer")),
        _ => Err(format!("missing required field '{key}'")),
    }
}

fn opt_str<'a>(body: &'a Value, key: &str) -> &'a str {
    body.get(key).and_then(Value::as_str).unwrap_or("")
}
