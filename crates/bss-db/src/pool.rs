//! Postgres connection pool setup.
//!
//! Mirrors the SQLAlchemy engine config the services share: `pool_size=5` +
//! `max_overflow=5` → up to 10 concurrent connections. sqlx replaces the async
//! engine; repositories hold a `PgPool` / `Transaction` and drop to raw SQL for
//! the correctness-critical paths (SKIP LOCKED, FOR UPDATE) exactly as today.

use sqlx::postgres::{PgPool, PgPoolOptions};

/// Persistent connections kept warm (SQLAlchemy `pool_size`).
pub const POOL_SIZE: u32 = 5;
/// Burst connections above [`POOL_SIZE`] (SQLAlchemy `max_overflow`).
pub const POOL_MAX_OVERFLOW: u32 = 5;

/// Connect a pool with the standard 5+5 config. `min_connections` mirrors the
/// persistent pool; `max_connections` is `pool_size + max_overflow`.
pub async fn connect(database_url: &str) -> Result<PgPool, sqlx::Error> {
    PgPoolOptions::new()
        .max_connections(POOL_SIZE + POOL_MAX_OVERFLOW)
        .min_connections(POOL_SIZE)
        .connect(database_url)
        .await
}
