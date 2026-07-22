//! Postgres connection pool setup.
//!
//! Mirrors the SQLAlchemy engine config the services share: `pool_size=5` +
//! `max_overflow=5` → up to 10 concurrent connections. sqlx replaces the async
//! engine; repositories hold a `PgPool` / `Transaction` and drop to raw SQL for
//! the correctness-critical paths (SKIP LOCKED, FOR UPDATE) exactly as today.
//!
//! ## Connection-leak defence (3 layers)
//!
//! With ~11 pooled processes against a shared 100-slot Postgres, leaked
//! connections exhaust the cap and starve later-starting services. Three layers
//! keep the footprint bounded:
//!
//! 1. **`idle_timeout`** — a pooled connection idle past this is closed, so a
//!    burst above [`POOL_MIN_IDLE`] drains back down instead of pinning slots.
//! 2. **`max_lifetime`** — every connection is recycled on a fixed clock, so no
//!    single connection lives unbounded.
//! 3. **Server-side `idle_session_timeout`** — the backstop for the *orphan*
//!    case: when a container is SIGKILL'd (e.g. `docker restart`), its sockets
//!    can linger server-side as `idle` because the FIN/RST never reaches
//!    Postgres through the bridge NAT. The client can't reap what it no longer
//!    owns, so we ask the *server* to drop any session idle past this window.
//!    Set comfortably above `idle_timeout` so a live client always reaps first;
//!    the server only ever closes connections whose owner is already gone.

use std::str::FromStr;
use std::time::Duration;

use sqlx::postgres::{PgConnectOptions, PgPool, PgPoolOptions};

/// Persistent connections kept warm (SQLAlchemy `pool_size`).
pub const POOL_SIZE: u32 = 5;
/// Burst connections above [`POOL_SIZE`] (SQLAlchemy `max_overflow`).
pub const POOL_MAX_OVERFLOW: u32 = 5;

/// Warm-connection floor. Kept low (was 5) so 11 processes pin ~11 idle slots at
/// rest instead of ~55 — headroom under the shared 100-slot cap, and less to leak.
pub const POOL_MIN_IDLE: u32 = 1;

/// Client-side idle reap: a pooled connection above [`POOL_MIN_IDLE`] idle this
/// long is closed and its slot returned.
const IDLE_TIMEOUT: Duration = Duration::from_secs(300);
/// Hard recycle age — no connection outlives this regardless of activity.
const MAX_LIFETIME: Duration = Duration::from_secs(1800);
/// Fail an acquire that can't be satisfied in this window (surfaces as the
/// `PoolTimedOut` we saw on a saturated server, rather than hanging).
const ACQUIRE_TIMEOUT: Duration = Duration::from_secs(30);
/// Server-side backstop for orphaned sessions, in milliseconds (10 min). Must
/// stay above [`IDLE_TIMEOUT`] so a live pool reaps first and this only ever
/// reaps sessions whose client process is already gone.
const SERVER_IDLE_SESSION_TIMEOUT_MS: &str = "600000";

/// Connect a pool with the standard 5+5 config plus the leak-defence layers
/// documented above. `max_connections` is `pool_size + max_overflow`.
pub async fn connect(database_url: &str) -> Result<PgPool, sqlx::Error> {
    let opts = PgConnectOptions::from_str(database_url)?
        // Sent as a startup `-c idle_session_timeout=…`; Postgres closes any
        // session (not in a transaction) idle past this — the orphan backstop.
        .options([("idle_session_timeout", SERVER_IDLE_SESSION_TIMEOUT_MS)]);

    PgPoolOptions::new()
        .max_connections(POOL_SIZE + POOL_MAX_OVERFLOW)
        .min_connections(POOL_MIN_IDLE)
        .acquire_timeout(ACQUIRE_TIMEOUT)
        .idle_timeout(IDLE_TIMEOUT)
        .max_lifetime(MAX_LIFETIME)
        // Ping on checkout so a connection the server reaped (idle_session_timeout)
        // is transparently discarded and replaced instead of erroring a query.
        .test_before_acquire(true)
        .connect_with(opts)
        .await
}
