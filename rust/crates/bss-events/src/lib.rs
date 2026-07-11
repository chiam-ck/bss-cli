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
pub mod relay;
pub mod topology;

pub use consumer::{death_count, decide_retry, RetryAction, CLAIM_INBOX_SQL};
pub use event::{stage_event, DomainEvent};
pub use relay::{
    drain_batch, relay_mode, EventPublisher, OutboxRow, RelayMode, RowOutcome, DRAIN_SQL,
    MARK_FAIL_SQL, MARK_OK_SQL,
};
pub use topology::{
    main_queue_args, parked_queue_name, retry_queue_args, retry_queue_name, EXCHANGE_NAME,
    RETRY_EXCHANGE_NAME,
};
