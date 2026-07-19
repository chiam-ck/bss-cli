//! `bss admin migrate [--baseline]` — apply the sqlx schema migrations (Phase 8).
//!
//! New in the Rust stack (no Python port): the Alembic tree is frozen and
//! `rust/migrations/0001_baseline.sql` is the go-forward schema source, applied by
//! the sqlx [`Migrator`](bss_db::migrate). A single runner over `BSS_DB_URL`, like
//! Python's one `alembic upgrade head` — not per-service-at-startup.
//!
//! - default → apply pending migrations (fresh install / incremental).
//! - `--baseline` → stamp `0001` as already-applied WITHOUT running it, for an
//!   existing install whose schema Alembic already created.

use std::process::ExitCode;

/// `bss admin migrate` entrypoint. `baseline` selects the stamp path.
pub async fn run(baseline: bool) -> ExitCode {
    let raw = match std::env::var("BSS_DB_URL") {
        Ok(v) if !v.is_empty() => v,
        _ => {
            eprintln!("migrate: BSS_DB_URL is not set");
            return ExitCode::from(1);
        }
    };
    // sqlx speaks plain `postgres://` — drop the SQLAlchemy async dialect suffix
    // (same normalisation the services' `normalize_db_url` applies).
    let db_url = raw
        .replace("postgresql+asyncpg://", "postgres://")
        .replace("postgresql://", "postgres://");

    let pool = match bss_db::connect(&db_url).await {
        Ok(p) => p,
        Err(e) => {
            eprintln!("migrate: connect failed: {e}");
            return ExitCode::from(1);
        }
    };

    if baseline {
        match bss_db::migrate::stamp_baseline(&pool).await {
            Ok(()) => {
                println!("Baseline stamped as applied (existing install — schema not re-run).");
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("migrate: baseline stamp failed: {e}");
                ExitCode::from(1)
            }
        }
    } else {
        match bss_db::migrate::run(&pool).await {
            Ok(()) => {
                println!("Migrations applied.");
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("migrate: failed: {e}");
                ExitCode::from(1)
            }
        }
    }
}
