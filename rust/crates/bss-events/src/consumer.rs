//! Safe consumer — the retry/park decision + inbox dedup contract.
//!
//! Port of the decision logic in `bss_events.consumer.bind_consumer`. The lapin
//! consume loop lands with the conformance service; the pure parts — how many
//! times a message has cycled (`x-death`), whether to retry or park, and the
//! inbox claim SQL — are here and tested without a broker.

use serde_json::Value;

/// Inbox claim: insert the processed-event row in the handler's transaction,
/// keyed on `(event_id, consumer)`. `rowcount == 1` means newly claimed; a
/// conflict means a duplicate delivery to ack-and-skip.
pub const CLAIM_INBOX_SQL: &str = "\
    INSERT INTO {schema}.processed_event (event_id, consumer, processed_at) \
    VALUES ($1, $2, now()) \
    ON CONFLICT (event_id, consumer) DO NOTHING";

/// What to do with a message whose handler failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RetryAction {
    /// Nack without requeue → dead-letters to the retry queue (TTL) then returns.
    Retry { attempt: u32 },
    /// Out of budget → move to `<queue>.parked` and ack the original.
    Park,
}

/// Decide retry vs park from the death count. Mirrors
/// `if attempts >= max_retries: park else: nack` — note the boundary is `>=`.
pub fn decide_retry(attempts: u32, max_retries: u32) -> RetryAction {
    if attempts >= max_retries {
        RetryAction::Park
    } else {
        RetryAction::Retry {
            attempt: attempts + 1,
        }
    }
}

/// How many times a message has cycled through the retry queue, read from the
/// `x-death` header (`headers["x-death"][0]["count"]`). Absent/malformed → 0,
/// matching the Python `_death_count` fallbacks.
pub fn death_count(headers: &Value) -> u32 {
    headers
        .get("x-death")
        .and_then(|xd| xd.as_array())
        .and_then(|arr| arr.first())
        .and_then(|first| first.get("count"))
        .and_then(|c| c.as_u64())
        .map(|n| n as u32)
        .unwrap_or(0)
}

// ── lapin/sqlx binding (the deferred P2 safe consumer) ──────────────────────

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use futures_util::StreamExt;
use lapin::options::{BasicAckOptions, BasicNackOptions};
use lapin::types::AMQPValue;
use sqlx::PgPool;

use crate::MqChannel;

/// A message handler — runs its domain writes on the supplied connection (the
/// consumer's transaction, shared with the inbox claim so they commit atomically).
/// Returns `Err(reason)` to trigger retry/park; the handler must NOT commit.
pub type EventHandler = Arc<
    dyn for<'c> Fn(
            &'c mut sqlx::PgConnection,
            Value,
        ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'c>>
        + Send
        + Sync,
>;

/// Bind the retry/park topology for `queue_name` and drive the consume loop —
/// port of `bss_events.consumer.bind_consumer`. Processes deliveries **serially**
/// (each to completion before the next): this is the intended one-at-a-time
/// semantics and, unlike the Python consumer's concurrent aio-pika callbacks,
/// avoids the lost-update race two simultaneous handlers would have on a shared
/// aggregate row (see the SOM `handle_task_completed` note).
///
/// Per delivery: claim the inbox row (dedup on the relay's `message_id` =
/// `event_id`), run the handler on the same transaction, commit + ack on success;
/// on failure roll back and either nack-to-retry (dead-letters to the TTL retry
/// queue) or, once the retry budget is spent, park the message and ack.
#[allow(clippy::too_many_arguments)]
pub async fn bind_consumer(
    mq: Arc<MqChannel>,
    pool: PgPool,
    queue_name: &str,
    routing_key: &str,
    consumer_tag: &str,
    inbox_schema: &str,
    max_retries: u32,
    retry_backoff_ms: u64,
    handler: EventHandler,
) -> Result<(), lapin::Error> {
    let mut consumer = mq
        .bind_safe_consumer(queue_name, routing_key, consumer_tag, retry_backoff_ms)
        .await?;
    tracing::info!(queue = queue_name, routing_key, "mq.consumer.started");

    while let Some(delivery) = consumer.next().await {
        let delivery = match delivery {
            Ok(d) => d,
            Err(e) => {
                tracing::warn!(error = %e, "mq.delivery.error");
                continue;
            }
        };

        let message_id = delivery
            .properties
            .message_id()
            .as_ref()
            .map(|s| s.as_str().to_string());

        let body: Value = match serde_json::from_slice(&delivery.data) {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!(error = %e, queue = queue_name, "mq.consumer.bad_json");
                ack(&delivery, queue_name).await;
                continue;
            }
        };

        match process_one(
            &pool,
            inbox_schema,
            queue_name,
            &handler,
            message_id.as_deref(),
            body,
        )
        .await
        {
            Ok(_) => ack(&delivery, queue_name).await,
            Err(reason) => {
                let deaths = lapin_death_count(&delivery);
                match decide_retry(deaths, max_retries) {
                    RetryAction::Retry { attempt } => {
                        tracing::warn!(
                            queue = queue_name,
                            attempt,
                            max_retries,
                            error = %reason,
                            "mq.message.retry"
                        );
                        // Nack without requeue → dead-letters to the retry queue.
                        if let Err(e) = delivery
                            .nack(BasicNackOptions {
                                requeue: false,
                                ..Default::default()
                            })
                            .await
                        {
                            tracing::warn!(error = %e, "mq.nack.failed");
                        }
                    }
                    RetryAction::Park => {
                        tracing::error!(
                            queue = queue_name,
                            attempts = deaths,
                            error = %reason,
                            "mq.message.parked"
                        );
                        if let Err(e) = mq
                            .publish_parked(
                                queue_name,
                                &delivery.data,
                                message_id.as_deref(),
                                &reason,
                            )
                            .await
                        {
                            tracing::warn!(error = %e, "mq.park.failed");
                        }
                        ack(&delivery, queue_name).await;
                    }
                }
            }
        }
    }
    Ok(())
}

/// Outcome of processing one delivery through the handler transaction.
enum Processed {
    Handled,
    Duplicate,
}

/// Claim the inbox, run the handler, commit — all in one transaction. Any error
/// (claim / handler / commit) returns `Err(reason)` so the caller retries or parks
/// (matching the Python `try` that wraps the whole block).
async fn process_one(
    pool: &PgPool,
    inbox_schema: &str,
    consumer: &str,
    handler: &EventHandler,
    message_id: Option<&str>,
    body: Value,
) -> Result<Processed, String> {
    let mut tx = pool.begin().await.map_err(|e| e.to_string())?;

    // Inbox dedup — a redelivered event_id already present is a duplicate to skip.
    if let Some(eid) = message_id {
        if let Ok(uuid) = uuid::Uuid::parse_str(eid) {
            let sql = CLAIM_INBOX_SQL.replace("{schema}", inbox_schema);
            let res = sqlx::query(&sql)
                .bind(uuid)
                .bind(consumer)
                .execute(&mut *tx)
                .await
                .map_err(|e| e.to_string())?;
            if res.rows_affected() == 0 {
                tracing::info!(consumer, event_id = eid, "inbox.duplicate.skipped");
                let _ = tx.commit().await;
                return Ok(Processed::Duplicate);
            }
        }
    }

    handler(&mut tx, body).await?;
    tx.commit().await.map_err(|e| e.to_string())?;
    Ok(Processed::Handled)
}

async fn ack(delivery: &lapin::message::Delivery, queue: &str) {
    if let Err(e) = delivery.ack(BasicAckOptions::default()).await {
        tracing::warn!(error = %e, queue, "mq.ack.failed");
    }
}

/// Read the retry-cycle count from the lapin `x-death` header
/// (`x-death[0]["count"]`), the lapin-typed analogue of [`death_count`].
fn lapin_death_count(delivery: &lapin::message::Delivery) -> u32 {
    let Some(headers) = delivery.properties.headers() else {
        return 0;
    };
    let Some(AMQPValue::FieldArray(arr)) = headers.inner().get("x-death") else {
        return 0;
    };
    let Some(AMQPValue::FieldTable(first)) = arr.as_slice().first() else {
        return 0;
    };
    match first.inner().get("count") {
        Some(AMQPValue::LongLongInt(n)) => (*n).max(0) as u32,
        Some(AMQPValue::LongInt(n)) => (*n).max(0) as u32,
        Some(AMQPValue::ShortInt(n)) => (*n).max(0) as u32,
        _ => 0,
    }
}
