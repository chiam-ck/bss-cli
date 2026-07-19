//! Schema migrations via the sqlx migrator (Phase 8 — Alembic freeze → sqlx baseline).
//!
//! The Python Alembic tree (`packages/bss-models/alembic`) is frozen; its end-state
//! is captured as `rust/migrations/0001_baseline.sql`, and the sqlx [`Migrator`] is
//! the go-forward schema source for the all-Rust stack. A single runner
//! (`bss admin migrate`) applies it — NOT per-service-at-startup, mirroring the
//! Python model's one `alembic upgrade head`.
//!
//! - [`run`] applies all pending migrations (fresh install / future incremental).
//! - [`stamp_baseline`] records the baseline as already-applied WITHOUT running it,
//!   for an existing install whose schema Alembic already created.

use sqlx::migrate::Migrator;
use sqlx::PgPool;

/// The embedded migration set (`rust/migrations`, relative to this crate). `0001` is
/// the frozen Alembic end-state baseline; new schema changes land as `000N_*.sql`
/// siblings and `run` applies them in order.
pub static MIGRATOR: Migrator = sqlx::migrate!("../../migrations");

/// Apply all pending migrations. Idempotent: migrations already recorded in
/// `_sqlx_migrations` are skipped (checksum-verified against the embedded copy).
pub async fn run(pool: &PgPool) -> Result<(), sqlx::migrate::MigrateError> {
    MIGRATOR.run(pool).await
}

/// Stamp the baseline (migration `0001`) as already-applied **without executing its
/// SQL** — for an existing install whose schema was created by Alembic. Creates the
/// `_sqlx_migrations` ledger if absent and inserts the baseline row with the embedded
/// checksum, so a later [`run`] treats it as applied and only runs genuinely-new
/// migrations. Idempotent (`ON CONFLICT DO NOTHING`).
pub async fn stamp_baseline(pool: &PgPool) -> Result<(), sqlx::Error> {
    // Same DDL sqlx's migrator uses for its ledger, so a subsequent `run` finds it.
    sqlx::query(
        "CREATE TABLE IF NOT EXISTS _sqlx_migrations (
            version BIGINT PRIMARY KEY,
            description TEXT NOT NULL,
            installed_on TIMESTAMPTZ NOT NULL DEFAULT now(),
            success BOOLEAN NOT NULL,
            checksum BYTEA NOT NULL,
            execution_time BIGINT NOT NULL
        )",
    )
    .execute(pool)
    .await?;

    // The baseline is embedded at compile time, so it is always present; degrade to
    // a no-op rather than panic if the migration set is ever emptied.
    let Some(baseline) = MIGRATOR.iter().next() else {
        return Ok(());
    };
    sqlx::query(
        "INSERT INTO _sqlx_migrations
            (version, description, success, checksum, execution_time)
         VALUES ($1, $2, true, $3, 0)
         ON CONFLICT (version) DO NOTHING",
    )
    .bind(baseline.version)
    .bind(baseline.description.as_ref())
    .bind(baseline.checksum.as_ref())
    .execute(pool)
    .await?;
    Ok(())
}
