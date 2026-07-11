//! Domain-event staging — port of the services' `events/publisher.publish`.
//!
//! `stage_event` builds the `audit.domain_event` row the caller `INSERT`s in the
//! **same transaction** as the domain write (the outbox). It never publishes —
//! the [`crate::relay`] is the only publisher. Caller/tenant/channel/identity are
//! stamped from the current [`RequestCtx`]; the occurred-at from the scenario
//! clock; `published_to_mq` starts false.

use chrono::{DateTime, Utc};
use serde_json::{json, Value};

use bss_context::RequestCtx;

/// The staged outbox row. Mirrors the `DomainEvent` ORM columns that
/// `publisher.publish` sets (DB defaults fill `published_attempts` etc.).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DomainEvent {
    pub event_id: String,
    pub event_type: String,
    pub aggregate_type: String,
    pub aggregate_id: String,
    pub occurred_at: DateTime<Utc>,
    pub trace_id: Option<String>,
    pub actor: String,
    pub channel: String,
    pub tenant_id: String,
    pub service_identity: String,
    pub payload: Value,
    pub schema_version: i32,
    pub published_to_mq: bool,
}

/// Build a domain event to stage, stamping context off `ctx` and time off the
/// scenario clock. `payload` defaults to `{}` when `None` (Python `payload or {}`).
/// `trace_id` is `None` until the OTel bootstrap lands (conformance step).
pub fn stage_event(
    ctx: &RequestCtx,
    event_type: impl Into<String>,
    aggregate_type: impl Into<String>,
    aggregate_id: impl Into<String>,
    payload: Option<Value>,
) -> DomainEvent {
    DomainEvent {
        event_id: uuid::Uuid::new_v4().to_string(),
        event_type: event_type.into(),
        aggregate_type: aggregate_type.into(),
        aggregate_id: aggregate_id.into(),
        occurred_at: bss_clock::now(),
        trace_id: None,
        actor: ctx.actor.clone(),
        channel: ctx.channel.clone(),
        tenant_id: ctx.tenant.clone(),
        service_identity: ctx.service_identity.clone(),
        payload: payload.unwrap_or_else(|| json!({})),
        schema_version: 1,
        published_to_mq: false,
    }
}
