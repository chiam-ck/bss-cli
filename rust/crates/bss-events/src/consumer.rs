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
