//! RabbitMQ topology — a **frozen contract** (phases/2.0/00-STRATEGY.md §3.3).
//!
//! The exchange names, queue arguments, and retry/parked wiring must be
//! byte-identical to `bss_events.consumer` so a Rust service and a Python one can
//! share the same broker during the strangler migration. These functions return
//! the argument tables as data so they are assertable without a live broker; the
//! lapin declarations that consume them land with the conformance service.

/// Durable topic exchange every domain event is published to.
pub const EXCHANGE_NAME: &str = "bss.events";
/// Direct dead-letter exchange the retry queues hang off.
pub const RETRY_EXCHANGE_NAME: &str = "bss.events.retry";

pub const DEFAULT_MAX_RETRIES: u32 = 5;
pub const DEFAULT_RETRY_BACKOFF_MS: u64 = 5000;

/// Arguments for a main queue `q`: failures dead-letter to the retry exchange,
/// keyed by the queue's own name.
pub fn main_queue_args(queue_name: &str) -> Vec<(&'static str, String)> {
    vec![
        ("x-dead-letter-exchange", RETRY_EXCHANGE_NAME.to_string()),
        ("x-dead-letter-routing-key", queue_name.to_string()),
    ]
}

/// Arguments for `q.retry`: hold the message for `backoff_ms`, then dead-letter
/// it back to the main exchange under the **original** routing key → main queue.
pub fn retry_queue_args(routing_key: &str, backoff_ms: u64) -> Vec<(&'static str, String)> {
    vec![
        ("x-message-ttl", backoff_ms.to_string()),
        ("x-dead-letter-exchange", EXCHANGE_NAME.to_string()),
        ("x-dead-letter-routing-key", routing_key.to_string()),
    ]
}

/// The name of the terminal parked queue for a main queue.
pub fn parked_queue_name(queue_name: &str) -> String {
    format!("{queue_name}.parked")
}

/// The name of the retry queue for a main queue.
pub fn retry_queue_name(queue_name: &str) -> String {
    format!("{queue_name}.retry")
}
