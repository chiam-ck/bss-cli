# Session Handoff — start here on a fresh session

This is the cold-start guide for resuming the Rust migration. Read this first,
then [`PROGRESS.md`](PROGRESS.md) for the detailed log and [`00-STRATEGY.md`](00-STRATEGY.md)
for the why.

## Where we are (2026-07-12)

**Phase 2 (event-plane services) is COMPLETE and tagged `v2.0.0-phase.2`.**
(Before it: `v2.0.0-phase.1` rating pilot, `v2.0.0-phase.0` foundations.)

**mediation, provisioning-sim, and som** are ported and **cut over into the running
stack**, alongside rating from P1. The order pipeline now runs on an all-Rust event
plane against the Python catalog/com/subscription/crm/payment. This is also where
the deferred event-plane bindings landed: the **outbox relay tick loop**
(`bss_events::start_relay`) and the **safe retry/park consumer**
(`bss_events::bind_consumer`) — som runs both. New platform pieces: the `bss-admin`
crate (shared reset router), `SubscriptionClient` + `InventoryClient`, and
`bss_clock::isoformat` (the first R1 datetime-in-payload seam).

**The six event-plane hero scenarios pass against the confirmed Rust event plane**
(run directly with the overlay held) — including the two named exit criteria
(`new_activation_with_provisioning_retry`, `inventory_low_watermark_and_replenishment`)
and `customer_signup_and_exhaust`. The P1 "stall" turned out to be a **misrun** (no
provider-flip wrapper → Stripe charge never approved), not a code bug — the Python
event plane passes the same suite. Separately, the Rust port hardens a *real latent*
concurrent lost-update race in SOM's CFS `pendingTasks` RMW (serial consumer + `FOR
UPDATE`; noted for a Python backport). **Deployment gotcha:** `make scenarios-hero`
reverts the Rust som/provisioning-sim to Python (portal-self-serve's health-gated
`depends_on` + the distroless images having no `HEALTHCHECK` until P8) — validate
with `COMPOSE_FILE=…:docker-compose.rust.yml` or run api scenarios directly. See
PROGRESS Phase 2 for the full write-up.

All platform crates green against the live stack. A Rust binary interoperates
byte-for-byte with the running Python system — same Postgres, RabbitMQ (shared
durable queues + retry topology), Jaeger, token perimeter.

**Cutover model** (Decision D8 — per-service, not a Phase-8 big bang): the Rust
containers run via the `docker-compose.rust.yml` overlay. Bring the stack up with
the overlay to keep them: `docker compose -f docker-compose.yml -f
docker-compose.rust.yml up -d`. Drop the overlay to fall back to the Python oracle
for a golden diff. The overlay's "cut over so far" list is the running ledger —
now rating + mediation + provisioning-sim + som.

**Resuming? Start at Phase 3** (`catalog` + `com`) using [`PLAYBOOK.md`](PLAYBOOK.md)
as the step-by-step recipe. See [`03-PHASES.md`](03-PHASES.md) §Phase 3.

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
| bss-clients | ◐ | base + AuthProviders; Catalog/Subscription/Inventory done (P1–P2); 9 clients left |
| bss-telemetry | ✅ | redaction rules + semconv + OTel bootstrap (→ Jaeger) |
| bss-events | ✅ | staging + relay tick loop + safe consumer + topology (lapin/sqlx landed P2) |
| bss-admin | ✅ | shared `admin_reset_router` (new crate, P2) |

**Deferred by design** (they land with the services that first need them, so
they're tested against real behaviour rather than as untested scaffolding):
the remaining ~9 typed clients, the ~60 per-table model structs, and the redaction
**Layer** over live `tracing` fields (the rules exist; no service logs sensitive
fields yet). The lapin/sqlx event-plane wiring (relay + safe consumer) landed in P2.

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

## What to do next: Phase 3 — catalog + com

Follow [`PLAYBOOK.md`](PLAYBOOK.md) — now validated across four services. See
[`03-PHASES.md`](03-PHASES.md) §Phase 3. Highlights:

- **catalog** (~4.7k) — TMF620 read surface + admin writes + promotions +
  price/window logic. The fattest client-consumer surface, so its golden tests
  protect everyone downstream. This is where the remaining `CatalogClient` methods
  (list/active-price/promotions/admin) land, and where the ~60-column TMF offering
  shape gets pinned (R1-heavy — see the `bss_clock::isoformat` seam for the pattern).
- **com** (~2.8k) — ProductOrder FSM, decomposition hand-off to som (it publishes
  the `order.in_progress` that the **now-Rust** som consumes), price snapshot at
  order time (guard 5's producer side). com **runs the relay + safe consumer** too
  — both bindings already exist in `bss-events` from P2, so com just wires them.
- Exit criteria: catalog-versioning/plan-change + order hero scenarios green; the
  Python portals still work against the now-mostly-Rust service plane. Tag
  `v2.0.0-phase.3`.

Reference to copy from: `rust/services/som/` (relay + safe-consumer wiring, graph
repo, event staging), `rust/services/mediation/` (typed client + first table
write), and any service's `tests/live_smoke.rs` (the live-proof pattern).

**One carry-over for Phase 3+ (or a spare-cycle Python backport):** SOM's
`handle_task_completed` concurrent lost-update race on the CFS `pendingTasks` JSONB
(root-caused in P2 — see PROGRESS). The Rust port fixed it (serial consumer + `FOR
UPDATE`); the Python oracle still has it. com has an analogous multi-event reaction
surface — port it with the same serialize/lock discipline.

## Quick pointers

- **Per-service porting recipe: [`PLAYBOOK.md`](PLAYBOOK.md)** (use this for every P2+ service)
- Detailed running log + tagging discipline: [`PROGRESS.md`](PROGRESS.md)
- Strategy / frozen contracts / what doesn't port: [`00-STRATEGY.md`](00-STRATEGY.md)
- Python "before" baseline for motto #6 (re-measured at Phase 8): [`05-BASELINE.md`](05-BASELINE.md)
- Rust workspace overview + commands: [`../../rust/README.md`](../../rust/README.md)
