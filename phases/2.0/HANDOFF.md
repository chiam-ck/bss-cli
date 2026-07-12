# Session Handoff — start here on a fresh session

This is the cold-start guide for resuming the Rust migration. Read this first,
then [`PROGRESS.md`](PROGRESS.md) for the detailed log and [`00-STRATEGY.md`](00-STRATEGY.md)
for the why.

## Where we are (2026-07-12)

**Phase 3 (catalog + com) is COMPLETE and tagged `v2.0.0-phase.3`.**
(Before it: `v2.0.0-phase.2` event plane, `v2.0.0-phase.1` rating, `v2.0.0-phase.0`
foundations.)

**catalog and com** are ported and **cut over into the running stack**. The service
plane is now Rust for rating + the event plane (P2) + catalog + com; only
subscription/crm/payment remain Python. New platform pieces this phase:
`rust_decimal` money (the P3 R1 seam — money columns read as `amount::text` →
`Decimal`, so `apply_discount`/`discount_label` match Python `Decimal`
byte-for-byte); a **second datetime seam** — TMF response bodies render `Z`
(Pydantic v2) vs the event-payload `+00:00` `bss_clock::isoformat`; the
`Decimal(str(float))` seed-string subtlety (a JSON float `25.0` → `Value::to_string()`
"25.0", not "25"); and six new typed clients / methods (`LoyaltyClient`,
`CrmClient`, `PaymentClient`, `SomClient`, `CatalogClient::{get_active_price,
validate_promo, resolve_eligible_promo}`, `SubscriptionClient::create`). com runs
the **relay + two safe consumers** (already-existing P2 bindings) + the
reconciliation sweeper; the SOM P2 lock/serialize lesson is applied (order read
`FOR UPDATE` in the consumer handlers).

**Catalog was golden-diffed** against the live Python oracle across 20+ endpoints
(TMF620 offering/price/spec, VAS, TMF671 promotions, and the live-loyalty promo
reads) — byte-identical (`Value ==`). **com's read surface** was golden-diffed too.
**All six P3 hero scenarios pass** against the confirmed all-Rust order plane (run
directly with the overlay held) — both named exit criteria
(`catalog_versioning_and_plan_change`, `new_activation_with_provisioning_retry`)
plus signup/exhaust, roaming add + use, and auto-renewal.

Loyalty-cli **is enabled** in this stack (`BSS_LOYALTY_API_TOKEN` set, pointing at
`agentic-vm` over Tailscale), so the promotion saga runs live — catalog and com
each hold their own `LoyaltyClient` (token never leaves the process).

**Deployment gotcha (unchanged):** `make scenarios-hero` reverts the Rust services to
Python (portal-self-serve's health-gated `depends_on` + no `HEALTHCHECK` until P8) —
validate with `COMPOSE_FILE=docker-compose.yml:docker-compose.rust.yml` held (the
provider-flip recreate then keeps the Rust images) or run scenarios directly. See
PROGRESS Phase 3 for the full write-up.

### Earlier: Phase 2 (event-plane services) — tagged `v2.0.0-phase.2`

**mediation, provisioning-sim, som** ported + cut over. This is where the deferred
event-plane bindings landed: the **outbox relay** (`bss_events::start_relay`) and the
**safe retry/park consumer** (`bss_events::bind_consumer`). Plus `bss-admin`,
`SubscriptionClient`/`InventoryClient`, `bss_clock::isoformat` (first R1 datetime
seam). The Rust port hardens a *real latent* concurrent lost-update race in SOM's CFS
`pendingTasks` RMW (serial consumer + `FOR UPDATE`; **Python backport still owed** —
com's analogous handlers already apply the same discipline).

All platform crates green against the live stack. A Rust binary interoperates
byte-for-byte with the running Python system — same Postgres, RabbitMQ (shared
durable queues + retry topology), Jaeger, token perimeter.

**Cutover model** (Decision D8 — per-service, not a Phase-8 big bang): the Rust
containers run via the `docker-compose.rust.yml` overlay. Bring the stack up with
the overlay to keep them: `docker compose -f docker-compose.yml -f
docker-compose.rust.yml up -d`. Drop the overlay to fall back to the Python oracle
for a golden diff. The overlay's "cut over so far" list is the running ledger —
now rating + mediation + provisioning-sim + som + catalog + com.

**Resuming? Start at Phase 4** (`payment` → `subscription` → `crm`) using
[`PLAYBOOK.md`](PLAYBOOK.md) as the step-by-step recipe. See
[`03-PHASES.md`](03-PHASES.md) §Phase 4 — the big three, each its own cutover.

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
| bss-clients | ◐ | base + AuthProviders; Catalog/Subscription/Inventory/Loyalty/Crm/Payment/Som (P1–P3); ~5 clients left, each partial to the calls used |
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

## What to do next: Phase 4 — payment → subscription → crm (the big three)

Follow [`PLAYBOOK.md`](PLAYBOOK.md) — now validated across six services. See
[`03-PHASES.md`](03-PHASES.md) §Phase 4. Ordered by blast-radius growth; each is its
own mini-project with its own cutover. Highlights:

- **payment** (~5.4k) — Stripe via direct reqwest (D4), tokenizer trait +
  constructor injection (grep guard: no direct mock charge), idempotency-key rules,
  webhook reconciliation (dedupe on `(provider,event_id)`, last-write-wins per
  intent, drift events), the server-side `tokenize`-raises rule. `PaymentClient` is
  currently partial (only `list_methods` from P3) — the rest of the surface lands
  here. **The money seam is already solved** — reuse `rust_decimal` + the
  `amount::text` read pattern from catalog/com.
- **subscription** (~6.2k) — highest correctness stakes (double-billing + quota
  math). Renewal worker (tick loop, three sweeps, mark-before-dispatch, SKIP
  LOCKED), balance decrement under `FOR UPDATE`, price-snapshot renewal, plan-change
  via pending fields, block-on-exhaust, VAS. Port the hypothesis balance suite as
  proptests. `SubscriptionClient` is partial (`get_by_msisdn` + `create`).
- **crm** (~7.4k) — 12 routers incl. Inventory pools, 4 FSMs, Case/Ticket
  invariants, KYC attestation verification, port-request aggregate. `CrmClient` is
  partial (`get_customer`).
- Exit criteria: **all 19 hero scenarios green with an all-Rust service plane** and
  all-Python portals/orchestrator/CLI. This is the bilingual resting point. Tag
  `v2.0.0-phase.4`.

Reference to copy from: `rust/services/catalog/` (money via `rust_decimal` +
`amount::text`, the TMF `Z` datetime formatter, optional `LoyaltyClient`, golden-diff
live smoke) and `rust/services/com/` (relay + two safe consumers + reconciliation
sweeper, promo consume lifecycle, order `FOR UPDATE` in handlers, `Decimal(str(float))`
seed-string seam).

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
