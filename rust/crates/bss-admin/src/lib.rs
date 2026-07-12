//! bss-admin — shared admin-api reset router factory.
//!
//! Rust port of `packages/bss-admin`. Each BSS service mounts its own
//! [`admin_reset_router`] with a hardcoded list of [`ResetPlan`]s — one per
//! Postgres schema it owns — under `/admin-api/v1`. The CLI (`bss admin reset`)
//! coordinates the per-service wipe across the stack; scenario runs call it after
//! `operational_data_reset` so each run starts from a clean slate.
//!
//! Every plan declares its schema once and the handler quotes `"schema"."table"`
//! directly, so a misconfigured plan cannot reach into another service's schema.
//! The endpoint is gated behind `BSS_ALLOW_ADMIN_RESET` (unset in production →
//! 403), read per-request to match the Python `os.environ.get` semantics (the
//! flag is a non-secret preference a scenario container toggles).
#![forbid(unsafe_code)]

mod reset;

pub use reset::{admin_reset_router, ResetMode, ResetPlan, TableReset};
