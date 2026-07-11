//! bss-events tests — ports the intent of `test_relay.py` + `test_consumer.py`
//! (drain/publish, retry-vs-park, dedup) plus pins the frozen topology + SQL
//! contracts. No broker/DB: the relay drains against a fake publisher.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::cell::RefCell;

use bss_context::RequestCtx;
use bss_events::{
    death_count, decide_retry, drain_batch, main_queue_args, parked_queue_name, relay_mode,
    retry_queue_args, retry_queue_name, stage_event, EventPublisher, OutboxRow, RelayMode,
    RetryAction, RowOutcome, DRAIN_SQL, EXCHANGE_NAME, RETRY_EXCHANGE_NAME,
};
use serde_json::json;

// ─── topology contract ──────────────────────────────────────────────────────

#[test]
fn topology_names_and_args_are_frozen() {
    assert_eq!(EXCHANGE_NAME, "bss.events");
    assert_eq!(RETRY_EXCHANGE_NAME, "bss.events.retry");
    assert_eq!(retry_queue_name("som.orders"), "som.orders.retry");
    assert_eq!(parked_queue_name("som.orders"), "som.orders.parked");

    // Main queue dead-letters to the retry exchange keyed by its own name.
    assert_eq!(
        main_queue_args("som.orders"),
        vec![
            ("x-dead-letter-exchange", "bss.events.retry".to_string()),
            ("x-dead-letter-routing-key", "som.orders".to_string()),
        ]
    );
    // Retry queue: TTL, then back to the main exchange under the ORIGINAL key.
    assert_eq!(
        retry_queue_args("order.in_progress", 5000),
        vec![
            ("x-message-ttl", "5000".to_string()),
            ("x-dead-letter-exchange", "bss.events".to_string()),
            ("x-dead-letter-routing-key", "order.in_progress".to_string()),
        ]
    );
}

#[test]
fn drain_sql_is_skip_locked_and_ordered() {
    assert!(DRAIN_SQL.contains("FOR UPDATE SKIP LOCKED"));
    assert!(DRAIN_SQL.contains("ORDER BY occurred_at ASC, id ASC"));
    assert!(DRAIN_SQL.contains("WHERE NOT published_to_mq"));
}

// ─── consumer decision logic ────────────────────────────────────────────────

#[test]
fn retry_until_budget_then_park() {
    assert_eq!(decide_retry(0, 5), RetryAction::Retry { attempt: 1 });
    assert_eq!(decide_retry(4, 5), RetryAction::Retry { attempt: 5 });
    // Boundary is >= : at max, park.
    assert_eq!(decide_retry(5, 5), RetryAction::Park);
    assert_eq!(decide_retry(9, 5), RetryAction::Park);
}

#[test]
fn death_count_reads_x_death_or_defaults_zero() {
    assert_eq!(death_count(&json!({})), 0);
    assert_eq!(death_count(&json!({"x-death": []})), 0);
    assert_eq!(death_count(&json!({"x-death": [{"count": 3}]})), 3);
    // Malformed shapes fall back to 0.
    assert_eq!(death_count(&json!({"x-death": "nope"})), 0);
    assert_eq!(death_count(&json!({"x-death": [{"no_count": 1}]})), 0);
}

// ─── relay off-mode ─────────────────────────────────────────────────────────

#[test]
fn relay_off_when_mq_unset() {
    assert_eq!(relay_mode(None), RelayMode::Off);
    assert_eq!(relay_mode(Some("")), RelayMode::Off);
    assert_eq!(relay_mode(Some("amqp://mq")), RelayMode::On);
}

// ─── relay drain orchestration (fake publisher) ─────────────────────────────

#[derive(Default)]
struct FakePublisher {
    sent: RefCell<Vec<(String, String, Vec<u8>)>>,
    fail_routing_keys: Vec<String>,
}

impl EventPublisher for FakePublisher {
    async fn publish(
        &self,
        routing_key: &str,
        message_id: &str,
        body: Vec<u8>,
    ) -> Result<(), String> {
        if self.fail_routing_keys.iter().any(|k| k == routing_key) {
            return Err("broker down".to_string());
        }
        self.sent
            .borrow_mut()
            .push((routing_key.to_string(), message_id.to_string(), body));
        Ok(())
    }
}

fn row(id: i64, event_id: &str, event_type: &str, payload: serde_json::Value) -> OutboxRow {
    OutboxRow {
        id,
        event_id: event_id.to_string(),
        event_type: event_type.to_string(),
        payload,
    }
}

#[tokio::test]
async fn drain_publishes_and_marks_each_row() {
    let rows = vec![
        row(1, "evt-1", "order.submitted", json!({"orderId": "ORD-1"})),
        row(2, "evt-2", "order.completed", json!(null)), // null payload → {}
    ];
    let pubr = FakePublisher::default();
    let outcomes = drain_batch(&rows, &pubr).await;

    assert_eq!(
        outcomes,
        vec![
            RowOutcome::Published { id: 1 },
            RowOutcome::Published { id: 2 }
        ]
    );
    let sent = pubr.sent.borrow();
    // routing_key = event_type, message_id = event_id, body = JSON payload.
    assert_eq!(sent[0].0, "order.submitted");
    assert_eq!(sent[0].1, "evt-1");
    assert_eq!(sent[0].2, br#"{"orderId":"ORD-1"}"#.to_vec());
    // null payload published as empty object.
    assert_eq!(sent[1].2, b"{}".to_vec());
}

#[tokio::test]
async fn drain_records_failure_without_marking_published() {
    let rows = vec![
        row(1, "evt-1", "ok.type", json!({})),
        row(2, "evt-2", "bad.type", json!({})),
    ];
    let pubr = FakePublisher {
        fail_routing_keys: vec!["bad.type".to_string()],
        ..Default::default()
    };
    let outcomes = drain_batch(&rows, &pubr).await;

    assert_eq!(outcomes[0], RowOutcome::Published { id: 1 });
    match &outcomes[1] {
        RowOutcome::Failed { id, error } => {
            assert_eq!(*id, 2);
            assert_eq!(error, "broker down");
        }
        other => panic!("expected Failed, got {other:?}"),
    }
    // Only the successful row was actually sent (at-least-once: failed retries).
    assert_eq!(pubr.sent.borrow().len(), 1);
}

// ─── staging ────────────────────────────────────────────────────────────────

#[test]
fn stage_event_stamps_context_and_defaults() {
    let ctx = RequestCtx {
        actor: "alice".to_string(),
        channel: "cli".to_string(),
        tenant: "DEFAULT".to_string(),
        service_identity: "portal_self_serve".to_string(),
        ..RequestCtx::default()
    };
    let e = stage_event(&ctx, "customer.created", "Customer", "CUST-001", None);

    assert_eq!(e.event_type, "customer.created");
    assert_eq!(e.aggregate_type, "Customer");
    assert_eq!(e.aggregate_id, "CUST-001");
    assert_eq!(e.actor, "alice");
    assert_eq!(e.channel, "cli");
    assert_eq!(e.tenant_id, "DEFAULT");
    assert_eq!(e.service_identity, "portal_self_serve");
    assert_eq!(e.payload, json!({})); // None → {}
    assert_eq!(e.schema_version, 1);
    assert!(!e.published_to_mq);
    assert_eq!(e.trace_id, None);
    // event_id is a fresh uuid (36 chars, hyphenated).
    assert_eq!(e.event_id.len(), 36);

    // Distinct events get distinct ids.
    let e2 = stage_event(&ctx, "customer.created", "Customer", "CUST-001", None);
    assert_ne!(e.event_id, e2.event_id);
}
