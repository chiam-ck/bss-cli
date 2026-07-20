//! bss-events — the transactional-outbox event plane.
//!
//! Rust port of `packages/bss-events`. Phase 0 lands the broker-free core: event
//! [`stage_event`]ing, the relay [`drain_batch`] orchestration + SQL, the safe
//! consumer's retry/park decision + inbox SQL, and the frozen RabbitMQ
//! [`topology`] contract. The lapin bindings (connect/declare/consume) and the
//! sqlx tick loop + `/audit-api/v1` router land with the hello-world conformance
//! service, where RabbitMQ + Postgres exist to validate them end to end.
#![forbid(unsafe_code)]

pub mod consumer;
pub mod event;
pub mod mq;
pub mod relay;
pub mod router;
pub mod topology;

pub use consumer::{
    bind_consumer, death_count, decide_retry, next_resubscribe_backoff, resubscribe_backoff,
    EventHandler, RetryAction, CLAIM_INBOX_SQL, INITIAL_RESUBSCRIBE_BACKOFF,
    MAX_RESUBSCRIBE_BACKOFF,
};
pub use event::{stage_event, DomainEvent};
pub use mq::MqChannel;
pub use relay::{
    drain_batch, drain_once, relay_mode, start_relay, EventPublisher, OutboxRow, Relay, RelayMode,
    RowOutcome, DRAIN_SQL, MARK_FAIL_SQL, MARK_OK_SQL,
};
pub use router::audit_events_router;
pub use topology::{
    main_queue_args, parked_queue_name, retry_queue_args, retry_queue_name, EXCHANGE_NAME,
    RETRY_EXCHANGE_NAME,
};
