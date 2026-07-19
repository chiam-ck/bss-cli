# Runbook — Rust schema baseline (`bss admin migrate`)

**Status:** Phase 8 (2.0 Rust migration). Replaces the Python `make migrate`
(`alembic upgrade head`) as the go-forward schema-management path for the all-Rust
stack.

## What changed

The Python Alembic tree (`packages/bss-models/alembic`, 32 migrations) is **frozen**.
Its end-state is captured as a single sqlx migration —
`migrations/0001_baseline.sql` — and the sqlx migrator, run by
**`bss admin migrate`**, is now the schema source of truth. Future schema changes land
as `migrations/000N_*.sql` siblings, applied in order by the same command.

`bss admin migrate` is a **single runner** over `BSS_DB_URL` (mirroring the one
`alembic upgrade head`) — the services do **not** migrate at startup. Applied state is
tracked in the `public._sqlx_migrations` ledger (checksum-verified).

## Prerequisites

- `BSS_DB_URL` set (the `postgresql+asyncpg://…` form is fine — the command normalises
  the scheme to `postgres://` internally).
- **pgvector.** The `knowledge` schema uses the `vector` type, so the baseline runs
  `CREATE EXTENSION IF NOT EXISTS vector`. The Postgres instance must have pgvector
  available (e.g. the `pgvector/pgvector:pg16` image, or `CREATE EXTENSION` privilege
  for a superuser on first apply).

## Fresh install (empty database)

```bash
bss admin migrate
```

Applies every pending migration (the baseline + any `000N_*.sql`). Idempotent — a
second run is a no-op. Creates all 15 domain schemas + `public`, 64 tables, and the
`vector` extension.

## Existing install (schema already created by Alembic)

An existing database — e.g. the shared `tech-vm` Postgres that Alembic has already
migrated — already has the full schema. Running a plain `bss admin migrate` there
would try to re-create it and **fail** (`schema "audit" already exists`). Instead,
**stamp** the baseline as already-applied, once, without re-running its SQL:

```bash
bss admin migrate --baseline
```

This creates the `_sqlx_migrations` ledger (if absent) and records `0001` as applied
(with the embedded checksum). After that, plain `bss admin migrate` is a no-op and will
apply only genuinely-new future migrations.

> One-time cutover step per existing database. The old `public.alembic_version` table
> is left untouched (harmless; it is just no longer consulted).

## Adding a future migration

1. Write `migrations/0002_<description>.sql` (plain SQL; schema-qualify objects).
2. `cargo build -p bss-cli` — the `sqlx::migrate!` macro re-embeds the `migrations/`
   dir at compile time, so the binary must be rebuilt after adding/editing a file.
3. `bss admin migrate` applies it (and skips `0001`).

## Notes / gotchas

- The baseline was captured with `pg_dump --schema-only --no-owner --no-privileges`
  (PostgreSQL 16), then post-processed: `public.alembic_version` excluded, the psql
  `\restrict`/`\unrestrict` meta-commands removed (sqlx executes SQL, not psql), and
  the `set_config('search_path','')` reset removed (all objects are schema-qualified,
  and an empty search_path otherwise hides sqlx's own `_sqlx_migrations` ledger from
  its post-migration bookkeeping). Do not hand-edit the file; regenerate + re-clean if
  the schema ever needs a fresh capture.
- Editing a migration that has already been applied elsewhere changes its checksum;
  sqlx will refuse to run against a database that recorded the old checksum. Add a new
  migration instead.
- greenlet/alembic are retired from the **Rust** runtime story; the Alembic tree stays
  in the archived Python repo as the historical record and for any lingering
  Python-stack database.
