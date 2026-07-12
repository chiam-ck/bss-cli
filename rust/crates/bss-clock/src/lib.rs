//! bss-clock — process-local scenario clock.
//!
//! Every BSS service uses [`now`] instead of calling `Utc::now()` directly. By
//! default [`now`] returns wall-clock UTC; scenarios can flip the process into
//! a frozen or offset mode via [`freeze`] / [`advance`] / [`unfreeze`].
//!
//! The state is deliberately per-process. Each service owns its own clock;
//! scenarios coordinate freeze/advance across services via the per-service
//! `/admin-api/v1/clock/*` admin endpoints ([`clock_admin_router`]).
//!
//! Rust port of `packages/bss-clock` (phases/2.0, Phase 0). The Python module
//! is the behavioural oracle; the tests here mirror `tests/test_clock.py`.
#![forbid(unsafe_code)]

mod clock;
mod router;

pub use clock::{
    advance, freeze, isoformat, now, parse_duration, reset_for_tests, state, unfreeze, ClockError,
    ClockState, Mode,
};
pub use router::clock_admin_router;
