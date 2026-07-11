//! Transactional-outbox relay — port of `bss_events.relay`.
//!
//! The relay is the **only** publisher (doctrine guard #17): a tick loop drains
//! unpublished `audit.domain_event` rows in `occurred_at` order under
//! `FOR UPDATE SKIP LOCKED`, publishes each with the durable `event_id` as the
//! AMQP `message_id`, and marks it published. Delivery is at-least-once — publish
//! precedes the mark, so a mark-commit failure re-publishes next tick.
//!
//! The SQL is exposed verbatim (the lapin/sqlx tick loop lands with the
//! conformance service). The [`drain_batch`] orchestration is testable now
//! against a fake [`EventPublisher`].

use std::future::Future;

use serde_json::{json, Value};

/// Drain query: unpublished rows, oldest first, lock-skipping for multi-replica.
pub const DRAIN_SQL: &str = "\
    SELECT id, event_id, event_type, payload \
    FROM audit.domain_event \
    WHERE NOT published_to_mq \
    ORDER BY occurred_at ASC, id ASC \
    LIMIT $1 \
    FOR UPDATE SKIP LOCKED";

/// Mark a row published (and bump the attempt counter).
pub const MARK_OK_SQL: &str = "\
    UPDATE audit.domain_event \
    SET published_to_mq = true, published_attempts = published_attempts + 1 \
    WHERE id = $1";

/// Record a publish failure without marking published (retried next tick).
pub const MARK_FAIL_SQL: &str = "\
    UPDATE audit.domain_event \
    SET published_attempts = published_attempts + 1, last_publish_error = $2 \
    WHERE id = $1";

/// Whether the relay runs. Mirrors `start_relay` returning `None` when `mq_url`
/// is unset — event delivery is off, but the durable audit log still records
/// everything for later replay.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RelayMode {
    On,
    Off,
}

/// `On` when an MQ url is configured, else `Off` (the durable log still records).
pub fn relay_mode(mq_url: Option<&str>) -> RelayMode {
    match mq_url {
        Some(u) if !u.is_empty() => RelayMode::On,
        _ => RelayMode::Off,
    }
}

/// One unpublished outbox row (the columns [`DRAIN_SQL`] selects).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutboxRow {
    pub id: i64,
    pub event_id: String,
    pub event_type: String,
    pub payload: Value,
}

/// What to do with a row after attempting to publish it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RowOutcome {
    /// Publish succeeded → apply [`MARK_OK_SQL`] for this id.
    Published { id: i64 },
    /// Publish failed → apply [`MARK_FAIL_SQL`] with the (truncated) error.
    Failed { id: i64, error: String },
}

/// Publishes one message to the exchange. The lapin impl lands with the
/// conformance service; tests use a fake.
pub trait EventPublisher {
    /// Publish `body` with `routing_key` (= `event_type`) and `message_id`
    /// (= `event_id`, the inbox dedup key).
    fn publish(
        &self,
        routing_key: &str,
        message_id: &str,
        body: Vec<u8>,
    ) -> impl Future<Output = Result<(), String>>;
}

/// Publish each row in order, mapping the result to a [`RowOutcome`]. The caller
/// applies [`MARK_OK_SQL`] / [`MARK_FAIL_SQL`] per outcome then commits — the
/// mark *follows* the publish, which is what makes delivery at-least-once.
pub async fn drain_batch<P: EventPublisher>(rows: &[OutboxRow], publisher: &P) -> Vec<RowOutcome> {
    let mut outcomes = Vec::with_capacity(rows.len());
    for row in rows {
        // Python `json.dumps(payload or {})` — a null/absent payload becomes {}.
        let payload = if row.payload.is_null() {
            json!({})
        } else {
            row.payload.clone()
        };
        let body = serde_json::to_vec(&payload).unwrap_or_else(|_| b"{}".to_vec());
        match publisher
            .publish(&row.event_type, &row.event_id, body)
            .await
        {
            Ok(()) => outcomes.push(RowOutcome::Published { id: row.id }),
            Err(e) => outcomes.push(RowOutcome::Failed {
                id: row.id,
                error: truncate(&e, 500),
            }),
        }
    }
    outcomes
}

/// Truncate to `max` chars, matching Python's `str(exc)[:500]`.
fn truncate(s: &str, max: usize) -> String {
    s.chars().take(max).collect()
}
