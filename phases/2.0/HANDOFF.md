# Session Handoff — start here on a fresh session

This is the cold-start guide for resuming the Rust migration. Read this first,
then [`PROGRESS.md`](PROGRESS.md) for the detailed log and [`00-STRATEGY.md`](00-STRATEGY.md)
for the why.

## Where we are (2026-07-11)

**Phase 1 (rating pilot) is COMPLETE and tagged `v2.0.0-phase.1`.**
(Phase 0 foundations before it: tag `v2.0.0-phase.0`.)

The first Python service (`rating`) is ported to Rust and **proven end-to-end
against the live stack**: the Rust rating service consumed a real `usage.recorded`
and emitted the `usage.rated` (audit row + published to MQ) via the live Catalog
and live Postgres. The **per-service porting playbook** — the real Phase-1
deliverable — is [`PLAYBOOK.md`](PLAYBOOK.md); it gets stamped 8 more times.

All 8 platform crates are built and green (now grown with `CatalogClient`, the
`/audit-api/v1` router, and the lapin `MqChannel`), proven against the live stack
via the P0 conformance harness + the P1 live smoke. A Rust binary interoperates
byte-for-byte with the running Python system — same Postgres, RabbitMQ, Jaeger,
token perimeter.

**Resuming? Start at Phase 2** (event-plane services: mediation, provisioning-sim,
som) using [`PLAYBOOK.md`](PLAYBOOK.md) as the step-by-step recipe. This is also
where the relay tick loop's lapin/sqlx binding lands (som/com/subscription run it).

- Work lives in [`../../rust/`](../../rust/) — a Cargo workspace (subtree of this
  monorepo; decision D7). The Python repo alongside stays the **oracle**.
- Branch: `2.0`. Commits are Conventional Commits; one annotated tag per phase
  (see the "Tagging discipline" section in `PROGRESS.md`).

## Environment (already set up on this machine)

- **Rust toolchain installed** at `~/.cargo` (stable 1.97, rustfmt + clippy).
  Every shell: `source "$HOME/.cargo/env"` first (cargo isn't on PATH by default).
- **Live infra is on `tech-vm`** (Postgres 5432, RabbitMQ 5672, Jaeger OTLP 4318 /
  query 16686), reachable from this host over Tailscale. The bss containers +
  their infra are already running.
- **Connection details** are in the repo-root `.env` (`BSS_DB_URL`, `BSS_MQ_URL`,
  `BSS_API_TOKEN`, `BSS_OTEL_*`). Never commit or print secrets.
- **No new infrastructure** was added and none is needed. sqlx/lapin/reqwest/otel
  are libraries compiled into the binaries; they reuse the existing infra. Rust
  *containers* replace Python ones only at Phase 8.

## Verify everything is green (do this first on resume)

```bash
source "$HOME/.cargo/env"
cd rust
cargo fmt --all --check                        # formatting gate
cargo clippy --all-targets --all-features -- -D warnings   # lint gate
cargo test                                     # 96 unit/integration tests (5 live smoke are #[ignore])

# Live conformance (needs the tech-vm stack up; never runs in CI):
set -a; source ../.env; set +a
cargo run -p conformance                       # 5 checks, all should PASS
```

## The 8 crates (all in `rust/crates/`)

| Crate | State | Note |
|---|---|---|
| bss-clock | ✅ | ArcSwap clock + admin router |
| bss-context | ✅ | RequestCtx + task-local + propagate layer |
| bss-middleware | ✅ | TokenMap (HMAC, golden vs oracle) + token gate |
| bss-db | ✅ | PolicyViolation (compiler-enforced 422) + sqlx pool |
| bss-models | ◐ | BSS_RELEASE only; per-table structs land per-service |
| bss-clients | ◐ | reqwest base + AuthProviders; 12 typed clients per-phase |
| bss-telemetry | ✅ | redaction rules + semconv + OTel bootstrap (→ Jaeger) |
| bss-events | ◐ | staging + drain + retry/park + topology; lapin/sqlx per-service |

**Deferred by design** (they land with the services that first need them, P1+, so
they're tested against real behaviour rather than as untested scaffolding):
the 12 typed clients, the lapin/sqlx service wiring (relay tick loop, consumer,
`/audit-api/v1` router), the ~60 per-table model structs, and the redaction
**Layer** over live `tracing` fields (the rules exist; no service logs yet).

## Load-bearing conventions (don't relearn these the hard way)

- **Behaviour-frozen until Phase 8.** The port changes zero external behaviour.
  New features go to the Python repo (the oracle), not here (risk R5).
- **Contracts pinned by golden vectors from the oracle.** See
  `bss-middleware/tests/golden_vectors.json` (HMAC). Do the same for each service's
  request/response/event JSON in Phase 1 (that's the "golden-contract rig").
- **Tests use real infra, not mocks**, mirroring the Python philosophy: a local
  axum peer for client tests, real Postgres for repo tests (Phase 1+).
- **Workspace lints:** `unsafe` forbidden; `unwrap`/`expect` denied in non-test
  code (test files carry `#![allow(clippy::unwrap_used, clippy::expect_used)]`).
- **Commit/tag/push only when the human asks.** `main` in the Python sense is the
  oracle; ship on `2.0`.

## What to do next: Phase 2 — event-plane services (mediation, provisioning-sim, som)

Follow [`PLAYBOOK.md`](PLAYBOOK.md) — the validated recipe from the rating pilot —
for each of the three. See [`03-PHASES.md`](03-PHASES.md) §Phase 2. Highlights:

- **mediation** (~1.6k) — block-at-edge synchronous rating path + roaming-indicator
  purity (guard 11); it *calls* the rating surface, so its golden tests lean on P1.
- **provisioning-sim** (~1.5k) — fault injection + "stuck" state + a domain worker.
- **som** (~1.8k) — atomic MSISDN+eSIM reservation (calls crm-hosted Inventory) +
  CFS/RFS decomposition.
- **This is where the relay tick loop lands** — som/com/subscription run the
  outbox relay (rating inline-publishes and doesn't). Bind the lapin/sqlx tick
  loop (drain→publish→mark, SKIP LOCKED) in `bss-events` and validate it against
  the real retry/parked topology + the provisioning-retry-resilience hero scenario.
- Exit criteria: provisioning-retry-resilience + inventory hero scenarios green on
  the mixed stack; parked/retry verified by killing handlers mid-run. Tag
  `v2.0.0-phase.2`.

Rating pilot reference to copy from: `rust/services/rating/` (crate layout),
`rust/services/rating/tests/live_smoke.rs` (the live-proof pattern).

## Quick pointers

- **Per-service porting recipe: [`PLAYBOOK.md`](PLAYBOOK.md)** (use this for every P2+ service)
- Detailed running log + tagging discipline: [`PROGRESS.md`](PROGRESS.md)
- Strategy / frozen contracts / what doesn't port: [`00-STRATEGY.md`](00-STRATEGY.md)
- Python "before" baseline for motto #6 (re-measured at Phase 8): [`05-BASELINE.md`](05-BASELINE.md)
- Rust workspace overview + commands: [`../../rust/README.md`](../../rust/README.md)
