//! Event staging — port of `app.events.publisher.publish` (v1.2 stage-only).
//!
//! com never publishes inline: it stages the `audit.domain_event` row
//! (`published_to_mq = false`) in the caller's transaction and the outbox relay
//! delivers it after commit. Stamps `service_identity` from the context.

use bss_context::RequestCtx;
use bss_events::stage_event;
use serde_json::Value;
use sqlx::postgres::PgConnection;

/// Stage a domain event row on `conn` (the caller's transaction). No publish.
pub async fn stage(
    conn: &mut PgConnection,
    ctx: &RequestCtx,
    event_type: &str,
    aggregate_type: &str,
    aggregate_id: &str,
    payload: Value,
) -> Result<(), sqlx::Error> {
    let ev = stage_event(ctx, event_type, aggregate_type, aggregate_id, Some(payload));
    let event_uuid = uuid::Uuid::parse_str(&ev.event_id).unwrap_or_else(|_| uuid::Uuid::new_v4());
    sqlx::query(
        "INSERT INTO audit.domain_event \
         (event_id, event_type, aggregate_type, aggregate_id, occurred_at, actor, channel, \
          tenant_id, service_identity, payload, schema_version, trace_id, published_to_mq) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,false)",
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
    .bind(&ev.trace_id)
    .execute(conn)
    .await?;
    tracing::info!(event_type, aggregate_type, aggregate_id, "event.staged");
    Ok(())
}
