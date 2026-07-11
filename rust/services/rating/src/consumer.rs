//! `usage.recorded` consumer — port of `app.events.consumer`.
//!
//! Subscribes to `usage.recorded`, and per event: fetch the tariff from Catalog,
//! run the pure [`decide_usage_outcome`], then stage the audit row + inline-publish
//! the resulting `usage.rated` / `usage.rejected` (rating uses inline publish, not
//! the outbox relay — only subscription/com/som run the relay).
//!
//! Errors are caught per-delivery and logged; the delivery is always acked
//! (never requeued), exactly like the Python `async with message.process()` with
//! the handler swallowing `RatingError` / unexpected errors.

use std::sync::Arc;

use bss_clients::CatalogClient;
use bss_context::RequestCtx;
use bss_db::PgPool;
use bss_events::{stage_event, MqChannel};
use futures_util::StreamExt;
use lapin::options::BasicAckOptions;
use serde_json::Value;

use crate::domain::{decide_usage_outcome, require_offering_id, RatingError, UsageOutcome};

const QUEUE: &str = "rating.usage.recorded";
const ROUTING_KEY: &str = "usage.recorded";
const CONSUMER_TAG: &str = "rating";

/// Non-fatal per-delivery failure. Logged; the delivery is still acked.
#[derive(Debug)]
pub enum HandleError {
    Rating(RatingError),
    Catalog(bss_clients::ClientError),
    Db(sqlx::Error),
}

impl From<RatingError> for HandleError {
    fn from(e: RatingError) -> Self {
        HandleError::Rating(e)
    }
}
impl From<bss_clients::ClientError> for HandleError {
    fn from(e: bss_clients::ClientError) -> Self {
        HandleError::Catalog(e)
    }
}
impl From<sqlx::Error> for HandleError {
    fn from(e: sqlx::Error) -> Self {
        HandleError::Db(e)
    }
}

/// Bind the queue and drive the consume loop forever. Spawned as a background
/// task from `main`; returns only on an unrecoverable stream error.
pub async fn run(
    mq: Arc<MqChannel>,
    pool: PgPool,
    catalog: CatalogClient,
) -> Result<(), lapin::Error> {
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

        match serde_json::from_slice::<Value>(&delivery.data) {
            Ok(body) => {
                tracing::info!(
                    usage_event_id = str_field_log(&body, "usageEventId"),
                    subscription_id = str_field_log(&body, "subscriptionId"),
                    "mq.usage.recorded.received"
                );
                if let Err(err) = handle_usage_recorded(&body, &catalog, &pool, Some(&mq)).await {
                    log_handle_error(&body, &err);
                }
            }
            Err(e) => tracing::warn!(error = %e, "mq.usage.recorded.bad_json"),
        }

        // Always ack — the handler owns its own error handling; rating never requeues.
        if let Err(e) = delivery.ack(BasicAckOptions::default()).await {
            tracing::warn!(error = %e, "mq.ack.failed");
        }
    }
    Ok(())
}

/// Fetch tariff → decide → stage + publish. Separated from the loop so it can be
/// driven directly (live smoke, future integration tests). `mq = None` stages the
/// audit row without publishing (the "mq not configured" path).
pub async fn handle_usage_recorded(
    body: &Value,
    catalog: &CatalogClient,
    pool: &PgPool,
    mq: Option<&MqChannel>,
) -> Result<(), HandleError> {
    let offering_id = require_offering_id(body)?; // before any fetch (Python order)
    let tariff = catalog.get_offering(&offering_id).await?;
    let outcome = decide_usage_outcome(body, &tariff, &offering_id)?;
    stage_and_publish(pool, mq, &outcome).await?;
    tracing::info!(
        event_type = outcome.event_type,
        aggregate_id = outcome.aggregate_id.as_str(),
        "usage.outcome.emitted"
    );
    Ok(())
}

/// Stage the `audit.domain_event` row and inline-publish. Publish first, then
/// INSERT with the resolved `published_to_mq` flag — the same final DB state as
/// the Python "stage → publish → set flag → commit" (best-effort delivery backed
/// by the durable audit row). The consumer has no request context, so the row is
/// stamped from `RequestCtx::default()` — the Python `auth_context.current()`
/// default (`system` / `system` / `DEFAULT` / `default`).
async fn stage_and_publish(
    pool: &PgPool,
    mq: Option<&MqChannel>,
    outcome: &UsageOutcome,
) -> Result<(), sqlx::Error> {
    let ctx = RequestCtx::default();
    let ev = stage_event(
        &ctx,
        outcome.event_type,
        outcome.aggregate_type,
        &outcome.aggregate_id,
        Some(outcome.payload.clone()),
    );

    let mut published = false;
    if let Some(mq) = mq {
        match mq.publish_json(outcome.event_type, &outcome.payload).await {
            Ok(()) => published = true,
            Err(e) => {
                tracing::warn!(error = %e, event_type = outcome.event_type, "mq.publish.failed")
            }
        }
    }

    let event_uuid = uuid::Uuid::parse_str(&ev.event_id).unwrap_or_else(|_| uuid::Uuid::new_v4());
    sqlx::query(
        "INSERT INTO audit.domain_event \
         (event_id, event_type, aggregate_type, aggregate_id, occurred_at, actor, channel, \
          tenant_id, service_identity, payload, schema_version, published_to_mq) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
    )
    .bind(event_uuid)
    .bind(&ev.event_type)
    .bind(&ev.aggregate_type)
    .bind(&ev.aggregate_id)
    .bind(ev.occurred_at)
    .bind(&ev.actor)
    .bind(&ev.channel)
    .bind(&ev.tenant_id)
    .bind(&ev.service_identity)
    .bind(sqlx::types::Json(ev.payload.clone()))
    .bind(ev.schema_version as i16)
    .bind(published)
    .execute(pool)
    .await?;

    Ok(())
}

fn log_handle_error(body: &Value, err: &HandleError) {
    let ue = str_field_log(body, "usageEventId");
    match err {
        HandleError::Rating(e) => tracing::warn!(usage_event_id = ue, error = %e.0, "rating.error"),
        HandleError::Catalog(e) => {
            tracing::warn!(usage_event_id = ue, error = ?e, "rating.catalog_error")
        }
        HandleError::Db(e) => {
            tracing::warn!(usage_event_id = ue, error = %e, "rating.handler.db_error")
        }
    }
}

/// A `&str` view of a body field for structured logs (`""` when absent) — keeps
/// tracing field values as plain `&str`.
fn str_field_log<'a>(body: &'a Value, key: &str) -> &'a str {
    body.get(key).and_then(Value::as_str).unwrap_or("")
}
