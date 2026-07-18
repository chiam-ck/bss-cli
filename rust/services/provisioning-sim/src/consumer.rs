//! `provisioning.task.created` consumer — port of `app.events.consumer`.
//!
//! Binds the durable `provisioning-sim.task.created` queue to
//! `provisioning.task.created` and drives each message through the worker. The
//! consumer has no request context, so tasks are stamped from
//! `RequestCtx::default` (Python `auth_context.current()` default).
//!
//! Ack semantics mirror aio-pika's `async with message.process()`: ack on
//! success, reject-without-requeue on a handler error (the queue has no
//! dead-letter args, so a rejected message is dropped — same as the oracle).

use std::sync::Arc;

use bss_context::RequestCtx;
use bss_db::PgPool;
use bss_events::MqChannel;
use futures_util::StreamExt;
use lapin::options::{BasicAckOptions, BasicRejectOptions};
use serde_json::Value;

use crate::esim::EsimProvider;
use crate::worker::{process_task, TaskRequest, INBOX_CONSUMER};

const QUEUE: &str = "provisioning-sim.task.created";
const ROUTING_KEY: &str = "provisioning.task.created";
const CONSUMER_TAG: &str = "provisioning-sim";

/// Bind the queue and drive the consume loop forever. Spawned from `main`.
pub async fn run(mq: Arc<MqChannel>, pool: PgPool, esim: EsimProvider) -> Result<(), lapin::Error> {
    let mut consumer = mq
        .declare_and_bind(QUEUE, ROUTING_KEY, CONSUMER_TAG)
        .await?;
    tracing::info!(queue = QUEUE, "mq.consumer.started");

    while let Some(delivery) = consumer.next().await {
        let delivery = match delivery {
            Ok(d) => d,
            Err(e) => {
                tracing::warn!(error = %e, "mq.delivery.error");
                continue;
            }
        };

        // The relay stamps the outbox row's event_id as the AMQP message_id.
        let message_id = delivery
            .properties
            .message_id()
            .as_ref()
            .map(|s| s.as_str().to_string());

        // Inbox dedup pre-check. The consumer is serial (one delivery to completion
        // before the next), so a plain read is race-free; the claim itself is
        // written atomically with the task persist inside `process_task`. A claimed
        // event_id is a relay redelivery to skip — this is what stops a duplicate
        // publish from re-running the task and re-emitting `task.completed`.
        if let Some(eid) = &message_id {
            match already_processed(&pool, eid).await {
                Ok(true) => {
                    tracing::info!(event_id = %eid, "inbox.duplicate.skipped");
                    ack(&delivery).await;
                    continue;
                }
                Ok(false) => {}
                // A dedup-read failure shouldn't drop the message — fall through and
                // process (the atomic claim still guards the persist).
                Err(e) => tracing::warn!(error = %e, "inbox.check.failed"),
            }
        }

        let outcome = match serde_json::from_slice::<Value>(&delivery.data) {
            Ok(body) => handle(&body, &mq, &pool, esim, message_id.as_deref()).await,
            Err(e) => {
                tracing::warn!(error = %e, "mq.task_created.bad_json");
                Ok(()) // unparseable → drop (ack), nothing to process
            }
        };

        match outcome {
            Ok(()) => ack(&delivery).await,
            Err(e) => {
                tracing::warn!(error = %e, "mq.task_created.handler_error");
                // requeue=false → drop (the queue has no DLX), matching process().
                if let Err(e) = delivery.reject(BasicRejectOptions { requeue: false }).await {
                    tracing::warn!(error = %e, "mq.reject.failed");
                }
            }
        }
    }
    Ok(())
}

/// Destructure the message and run the worker. A missing required field is
/// treated as an unprocessable message (logged, dropped via ack in the caller).
async fn handle(
    body: &Value,
    mq: &Arc<MqChannel>,
    pool: &PgPool,
    esim: EsimProvider,
    event_id: Option<&str>,
) -> Result<(), sqlx::Error> {
    let (Some(service_id), Some(service_order_id), Some(task_type)) = (
        body.get("serviceId").and_then(Value::as_str),
        body.get("serviceOrderId").and_then(Value::as_str),
        body.get("taskType").and_then(Value::as_str),
    ) else {
        tracing::warn!("mq.task_created.missing_fields");
        return Ok(());
    };

    tracing::info!(
        service_id = service_id,
        task_type = task_type,
        "mq.task_created.received"
    );

    let ctx = RequestCtx::default();
    process_task(
        pool,
        Some(mq),
        esim,
        &ctx,
        TaskRequest {
            service_id: service_id.to_string(),
            service_order_id: service_order_id.to_string(),
            commercial_order_id: body
                .get("commercialOrderId")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            task_type: task_type.to_string(),
            payload: body
                .get("payload")
                .cloned()
                .unwrap_or_else(|| Value::Object(Default::default())),
        },
        event_id,
    )
    .await
}

/// Ack a delivery, logging (but not propagating) an ack failure.
async fn ack(delivery: &lapin::message::Delivery) {
    if let Err(e) = delivery.ack(BasicAckOptions::default()).await {
        tracing::warn!(error = %e, "mq.ack.failed");
    }
}

/// Has this `event_id` already been claimed by the provisioning consumer? A read
/// (not a claim) — safe because the consumer is serial. Non-UUID ids (there should
/// be none from the relay) are treated as not-processed.
async fn already_processed(pool: &PgPool, event_id: &str) -> Result<bool, sqlx::Error> {
    let Ok(uuid) = uuid::Uuid::parse_str(event_id) else {
        return Ok(false);
    };
    let hit: Option<i32> = sqlx::query_scalar(
        "SELECT 1 FROM provisioning.processed_event WHERE event_id = $1 AND consumer = $2",
    )
    .bind(uuid)
    .bind(INBOX_CONSUMER)
    .fetch_optional(pool)
    .await?;
    Ok(hit.is_some())
}
