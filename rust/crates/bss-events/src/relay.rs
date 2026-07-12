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

// ── lapin/sqlx tick loop (the deferred P2 binding) ──────────────────────────

use std::sync::Arc;

use sqlx::{PgPool, Row};

use crate::MqChannel;

/// [`EventPublisher`] backed by a live [`MqChannel`] — publishes each drained row
/// to `bss.events` with the durable `event_id` as the AMQP `message_id`.
struct LapinPublisher(Arc<MqChannel>);

impl EventPublisher for LapinPublisher {
    async fn publish(
        &self,
        routing_key: &str,
        message_id: &str,
        body: Vec<u8>,
    ) -> Result<(), String> {
        self.0
            .publish_bytes_with_id(routing_key, &body, message_id)
            .await
            .map_err(|e| e.to_string())
    }
}

/// A running outbox relay. Store it and call [`Relay::stop`] on shutdown.
pub struct Relay {
    handle: tokio::task::JoinHandle<()>,
}

impl Relay {
    /// Cancel the tick loop.
    pub async fn stop(self) {
        self.handle.abort();
        let _ = self.handle.await;
    }
}

/// Drain one batch: select unpublished rows `FOR UPDATE SKIP LOCKED`, publish each
/// (publish precedes the mark → at-least-once), apply the per-row mark, commit.
/// Returns the number of rows drained. The publish I/O happens while the rows are
/// locked, exactly like the Python relay holding its session open.
pub async fn drain_once(
    pool: &PgPool,
    mq: &Arc<MqChannel>,
    batch_size: i64,
) -> Result<usize, sqlx::Error> {
    let mut tx = pool.begin().await?;
    let db_rows = sqlx::query(DRAIN_SQL)
        .bind(batch_size)
        .fetch_all(&mut *tx)
        .await?;

    let rows: Vec<OutboxRow> = db_rows
        .iter()
        .map(|r| OutboxRow {
            id: r.get::<i64, _>("id"),
            event_id: r.get::<uuid::Uuid, _>("event_id").to_string(),
            event_type: r.get::<String, _>("event_type"),
            payload: r
                .try_get::<Option<Value>, _>("payload")
                .ok()
                .flatten()
                .unwrap_or(Value::Null),
        })
        .collect();

    let publisher = LapinPublisher(mq.clone());
    let outcomes = drain_batch(&rows, &publisher).await;

    for outcome in &outcomes {
        match outcome {
            RowOutcome::Published { id } => {
                sqlx::query(MARK_OK_SQL).bind(id).execute(&mut *tx).await?;
            }
            RowOutcome::Failed { id, error } => {
                tracing::warn!(id = id, error = %error, "outbox.relay.publish_failed");
                sqlx::query(MARK_FAIL_SQL)
                    .bind(id)
                    .bind(error)
                    .execute(&mut *tx)
                    .await?;
            }
        }
    }

    tx.commit().await?;
    Ok(rows.len())
}

/// Start the outbox relay as a background task — port of `start_relay`. Drains
/// every `interval_ms` (no wait when a full batch is drained, so a backlog
/// clears fast). A tick failure is logged and never kills the loop.
pub fn start_relay(pool: PgPool, mq: Arc<MqChannel>, interval_ms: u64, batch_size: i64) -> Relay {
    let handle = tokio::spawn(async move {
        tracing::info!(interval_ms, batch_size, "outbox.relay.started");
        loop {
            let drained = match drain_once(&pool, &mq, batch_size).await {
                Ok(n) => n,
                Err(e) => {
                    tracing::warn!(error = %e, "outbox.relay.tick_failed");
                    0
                }
            };
            // Back off only when idle; a full batch may have more waiting.
            if (drained as i64) < batch_size {
                tokio::time::sleep(std::time::Duration::from_millis(interval_ms)).await;
            }
        }
    });
    Relay { handle }
}
