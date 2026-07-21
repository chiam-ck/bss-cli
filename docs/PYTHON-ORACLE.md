# The Python oracle (retired at v2.0.0)

BSS-CLI was originally written in Python and rewritten to Rust (the 2.0 flip,
2026-07-19). Through the migration and the soak, the Python implementation was
kept in-tree at `python-legacy/` as the **reproducible oracle** for golden-diff
parity checks.

At the **v2.0.0 release (2026-07-21)** the oracle was retired from the working
tree. The all-Rust workspace at the repo root is the one and only implementation.

## Recovering the oracle

It is preserved two ways — nothing was lost:

1. **Git tag** — `python-oracle-final` points at the last commit where
   `python-legacy/` was in-tree. To browse or run it:
   ```bash
   git worktree add /tmp/bss-python-oracle python-oracle-final
   cd /tmp/bss-python-oracle/python-legacy   # the full Python tree + py-* dev loop
   ```
   (Full git history under `python-legacy/` remains reachable regardless.)

2. **Tarball** — `~/archives/bss-cli-python-legacy-v2.0.0-20260721.tar.gz`
   (operator-local, outside the repo). 27 MB expanded.

## What retired with it

The Python-only dev/test tooling that was never ported to Rust retired with the
oracle (recoverable via the tag/tarball above):

- The `py-*` Makefile targets (`py-test`, `py-fmt`, `py-lint`, `py-migrate`,
  `py-seed`, `py-doctrine-check`), `python-check`, `check-clock`, `lint-types`
  (mypy). The Rust dev loop (`make test/fmt/lint/doctrine-check/migrate/seed`)
  is canonical.
- `make e2e` / `e2e-batched` / `e2e-down` + `docker-compose.e2e.yml` — the
  Python Playwright/bss-e2e suite. The Rust ship gate is `make scenarios-hero`.
- **The demo seed** (`bss_seed.demo` → `make seed-demo` / `seed-demo-reset` /
  `loyalty-reset` / `demo-restore`). **Not yet ported.** Follow-up: add
  `bss admin seed-demo` as the Rust equivalent. Until then, seed demo data via
  the tag/tarball, or by hand (see the loyalty-cli promo runbook).

Alembic is retired; migrations are the sqlx baseline under `migrations/`.
