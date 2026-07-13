# Session Handoff — start here on a fresh session

This is the cold-start guide for resuming the Rust migration. Read this first,
then [`PROGRESS.md`](PROGRESS.md) for the detailed log and [`00-STRATEGY.md`](00-STRATEGY.md)
for the why.

## Where we are (2026-07-13)

**Phase 4 is COMPLETE — the ENTIRE SERVICE PLANE IS NOW RUST. Tagged
`v2.0.0-phase.4`.** All 8 backend services run Rust images: rating (`.1`), the event
plane mediation/provisioning-sim/som (`.2`), catalog + com (`.3`), and payment (4a) +
subscription (4b) + crm (4c). The **portals + orchestrator + CLI** remain Python.

**Phase 5 is IN PROGRESS — the Python LLM/lib side.** P5 is the first phase with
**no container cutover of its own** (D3): `bss-orchestrator`, `bss-knowledge`, and
`bss-cockpit` core are *library* crates that cut over in P6/P7 when the Rust
portals/CLI link them. The gate is **transcript parity**, not a hero-suite swap.
Split into **P5a `bss-knowledge`** (done), **P5b `bss-cockpit` core** (done), **P5c
`bss-orchestrator`** (started — multi-slice). Nothing is tagged yet — the
`v2.0.0-phase.5` tag caps the whole phase after P5c completes.

**P5c is multi-slice** (~7.2k Py LOC + 110 tools). **Slices 1–7 done (~48/110 tools):**
- **Slice 1** — the hand-rolled ReAct loop (`agent::astream_once`, replacing
  LangGraph), the `MockChatModel` fixture player, the guard stack (3-strike failure
  bail, identical-call stuck bail, destructive gating w/ batched/granular autonomy),
  the tool registry + profiles, and the `clock.*` pilot family.
- **Slice 2** — the **client-backed tool pattern** (the template for all remaining
  tools): a tool closure captures its typed `bss-clients` client, returns the response
  **verbatim**, maps `ClientError`→structured observation. Byte-parity follows
  transitively from the service golden diffs (P1–P4), so the gate is a live
  `tool == direct client call` smoke, not a re-diff against the Python tool. First
  family: the six **catalog reads** (extended `CatalogClient` with `list_offerings`/
  `list_vas`/`get_active_price_at`). Description golden + profile-membership pinned.
- **Slice 3** — the **CRM read family** (`customer.get`, `customer.list`,
  `customer.find_by_msisdn`, `customer.find_by_email`, `customer.get_kyc_status`,
  `interaction.list`). `customer.get` is the first **composite** — four parallel
  reads via `join4`, `return_exceptions`-style degrade to `[]`, `_extras` stitched on.
  Extended `CrmClient` with the six read methods; promoted `map_client_err`/`req_str`/
  `opt_str` to `tools/mod.rs` (shared helper kit). CRM reads are **operator_cockpit-
  only** (chat sees the `*.mine` wrappers, not these).
- **Slice 4** — the **subscription read family** (`subscription.get`,
  `list_for_customer`, `get_balance`, `get_esim_activation`). `get_esim_activation`
  is the first **projected-dict** tool, which forced **D9: `serde_json`
  `preserve_order` workspace-wide** so Rust matches Python's insertion order for both
  verbatim reserialization and `json!` literals (the R2 seam flagged in slice 3 — now
  closed; zero test breakage since the service goldens are `Value ==`). Extended
  `SubscriptionClient` with `get_balance`/`get_esim_activation`. D9 is pinned by the
  live smoke's serialized-key-order assertion.
- **Slice 5** — the **payment read family** (`payment.list_methods`,
  `payment.get_attempt`, `payment.list_attempts`), all verbatim. Extended
  `PaymentClient` with `get_payment`/`list_payments`. The live smoke surfaced that the
  list route requires `customerId` on both Python and Rust (faithful parity — the tool
  omits `None`, service 400s). Operator-only (chat sees the `payment.*_mine` wrappers).
- **Slice 6** — the **operator read BATCH** (17 tools: order, SOM, inventory,
  provisioning, usage, agents, events). Cadence switched to big batches. New clients:
  `ComClient` (+ the `order.wait_until` **polling composite**, which brought `tokio`
  into `bss-clients` deps), `ProvisioningClient`, `MediationClient`; extended
  `SomClient`/`InventoryClient`/`CrmClient`. `events.list` is the NOT_IMPLEMENTED stub
  (byte-exact message). One broad live smoke covers the batch. Operator-only.

- **Slice 7** — the **CRM/catalog read BATCH** (8 tools: ticket / case / promo /
  port_request, incl. the `case.show_transcript_for` composite). Extended `CrmClient`
  (get_case/get_chat_transcript/get_ticket/list_tickets/list_port_requests/
  get_port_request; `list_cases` widened with `agent_id`) + `CatalogClient::
  get_promotion`. Operator-only. One broad live smoke.

**Remaining P5c slices (batched — aim ~4):**
1. **Trace + knowledge reads** — `trace.get`/`for_order`/`for_subscription` (need a
   Jaeger client + an audit-events client + the `_summarize_trace` reducer) and
   `knowledge.search`/`knowledge.get` (a sqlx pool + the already-ported `bss-knowledge`
   crate's `search_fts`/`get_chunk` + the `BSS_KNOWLEDGE_ENABLED` gate + the search
   result-wrapping). Both infra-heavy — the last of the reads.
2. **Operator writes** (~45 tools) — customer/case/ticket/subscription/payment/order/
   promo/port_request/provisioning/inventory writes + catalog admin. All client calls;
   destructive gating already exists in `safety.rs`. Likely one big slice (or split
   money-movers out if it gets unwieldy).
3. **`customer_self_serve` `*.mine` wrappers** (~17) — the genuinely distinct one:
   auth-context actor binding (`ToolCtx.actor` + a `CHAT_NO_ACTOR_BOUND` error), an
   `assert_subscription_owned` ownership pre-check, `_annotate_pricing` (rust_decimal +
   `discount_label`). Reuses the now-ported Crm/Subscription/Payment/Mediation methods.
4. **OpenRouter `ChatModel` client** (reqwest direct) — a real model drives the loop.
5. **Ownership trip-wire** (`OWNERSHIP_PATHS`/`assert_owned_output`) + **chat caps** +
   `validate_profiles()`, and the **prompts** (`SYSTEM_PROMPT` + customer-chat; do NOT
   add ITERATIVE FLOW to customer chat — doctrine guard).

Keep descriptions/param docs byte-identical (R2); **schemars** arg schemas (D5) land
with the model client. The R2 fixture-corpus transcript-parity gate closes when the
tools + model client are in; then tag `v2.0.0-phase.5`.
2. **OpenRouter `ChatModel` client** (reqwest direct) — a real model drives the loop.
3. **Ownership trip-wire** (`OWNERSHIP_PATHS` / `assert_owned_output`) + **chat
   caps** (hourly + monthly-cost, fail-closed) + `validate_profiles()`.
4. **Prompts**: `SYSTEM_PROMPT` + the customer-chat prompt (verbatim; do NOT add
   the ITERATIVE FLOW block to customer chat — doctrine guard).
   The R2 fixture-corpus transcript-parity gate closes when the tools land.

- **P5a `bss-knowledge` ✅ ported.** `rust/crates/bss-knowledge`: chunker + FTS
  search + indexer. Chunker golden byte-for-byte vs the oracle across the three
  split policies (CI); the live `search_fts`/`get_chunk` diff byte-identical on the
  wire contract (`to_value` omits `rank`; `rank` came 1 ULP off on the `f32→f64`
  re-rank multiply — pinned within `1e-12`). See PROGRESS §Phase 5a.
- **P5b `bss-cockpit` core ✅ ported.** `rust/crates/bss-cockpit`: the Conversation
  store (`transcript_text` is the frozen contract P5c parses; chrome rows dropped),
  config mtime hot-reload + last-good fallback, and `build_cockpit_prompt` with the
  15.8 KB `COCKPIT_INVARIANTS` embedded byte-for-byte (`include_str!`, golden-pinned).
  Two seams handled: the verbatim invariants and pending-destructive **arg key-order**
  (stored `json`-column text order → `IndexMap` + `py_repr`). Deferred to P6/P7:
  the ASCII renderers, `strip_fake_propose` + `postprocess::*` (lookbehind/lookahead
  → `fancy-regex`), and the settings/branding writers (land with `bss-branding`).
  See PROGRESS §Phase 5b.

**Lesson carried from P5a/b: when the heavy lifting is a Postgres builtin (FTS, the
`json` column's text-order preservation), parity is structural — the risk is the
pure Rust around it (chunker algorithm, float widening, arg-order, `py_repr`).**

**Each of the big-three cut over with a read-surface golden diff + the hero suite
(15/19; the 4 failures are pre-existing portal/trace issues — branding text,
`/auth/check-email` 400, Jaeger `spanCount`).** Two cutover lessons worth carrying to
P5:
- **subscription (4b):** its Python `usage.rated` consumer used a *plain* queue (never
  migrated to the v1.2 safe-consumer pattern), so the orphaned queue had to be deleted
  for the Rust `bind_consumer` to redeclare it with the retry topology.
- **crm (4c):** the read-only golden diff missed a **write-body** bug — `POST
  /interaction` 422'd on camelCase `customerId` (the oracle's `TmfBase` accepts both
  cases). Caught by a direct endpoint probe when two LLM scenarios thrashed. **P5
  should exercise the write surface, not just reads.**
- **The hero harness flips `BSS_PAYMENT_PROVIDER→mock` for the run** (the scenarios use
  mock cards); running `bss scenario run-all` directly needs that flip done manually
  (recreate payment `--no-deps`), then restored to stripe.

---

### Phase 3 platform pieces (historical context, still current)

New platform pieces from P3:
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

## What to do next: finish Phase 5 (P5b → P5c), then Phase 6+ portals/CLI

Phase 5 is underway. **P5a `bss-knowledge` is ported; next is P5b `bss-cockpit`
core, then P5c `bss-orchestrator`.** See [`03-PHASES.md`](03-PHASES.md) §Phase 5
and PROGRESS §Phase 5.

- **P5b — `bss-cockpit` core** (`packages/bss-cockpit`, ~3.6k Py LOC): the
  `Conversation` store (`cockpit` schema — `transcript_text()` format is a frozen
  contract the orchestrator's `_messages_from_transcript` parses), the
  `pending_destructive` row (the `/confirm` contract), the chrome filter
  (`_ASSISTANT_CHROME_PREFIXES` — the inventory-lock test pins the set), prompt
  composition (`_COCKPIT_INVARIANTS` is code-defined, prepended verbatim), and
  `settings.toml` hot-reload (mtime cache; `toml_edit` for writes). The ASCII
  **renderers** can defer to P6/P7 (land with the first browser/CLI consumer).
  Golden-diff the transcript format + pending_destructive rows.
- **P5c — `bss-orchestrator`** (~7.2k Py LOC): the biggest. Hand-roll the ReAct
  loop (no LangGraph — system prompt + messages → OpenRouter → run tool_calls →
  append ToolMessage → repeat). Port the guard stack 1:1 (`safety.wrap_destructive`
  + autonomy `LoopState`; the 3-strike failure + `_IdenticalCallTracker` bails;
  `ownership.assert_owned_output`; `chat_caps`). Keep **tool descriptions/param
  docstrings byte-identical** (R2 — they drive model behaviour). Reimplement
  `MockChatModel` (substring-match on latest user text → walk `steps`) so the
  fixture corpus replays event-identically. Port tools profile-by-profile,
  `customer_self_serve` first (smaller, ownership-critical). **Don't** add the
  ITERATIVE FLOW block to customer chat (doctrine guard).

Then Phase 6+ (portals self-serve 9001 + cockpit 9002, then CLI):

- The **4 standing hero failures** are all portal/trace (branding text,
  `/auth/check-email` 400, Jaeger `spanCount`) — they land in the P6 portal port and
  are the natural first acceptance target (get to 19/19).
- **Exercise write bodies, not just reads,** when validating a port (the 4c
  interaction-camelCase lesson). A read golden diff is necessary, not sufficient.
- All the service seams are solved and reusable: money (`rust_decimal` +
  `amount::text`), the two datetime seams (`Z` responses / `+00:00` events), the
  safe-consumer + relay bindings, the stage-only publisher, `bss-clients` typed
  clients (now broad across all 8 services), and the per-service cutover playbook.

Reference to copy from: `rust/services/catalog/` (money via `rust_decimal` +
`amount::text`, the TMF `Z` datetime formatter, optional `LoyaltyClient`, golden-diff
live smoke) and `rust/services/com/` (relay + two safe consumers + reconciliation
sweeper, promo consume lifecycle, order `FOR UPDATE` in handlers, `Decimal(str(float))`
seed-string seam).

**Carry-over — DONE (2026-07-12):** SOM's `handle_task_completed` concurrent
lost-update race on the CFS `pendingTasks` JSONB (root-caused in P2 — see PROGRESS)
is now backported to the Python oracle. `ServiceRepository.get_for_update`
(`SELECT ... FOR UPDATE`) is used in all three task handlers (completed/failed/stuck),
mirroring the Rust `get_service_for_update` fix; regression pinned by
`services/som/tests/test_task_completion_locking.py` (two-connection lock-timeout
proof). The oracle and Rust port now agree on this invariant.

## Quick pointers

- **Per-service porting recipe: [`PLAYBOOK.md`](PLAYBOOK.md)** (use this for every P2+ service)
- Detailed running log + tagging discipline: [`PROGRESS.md`](PROGRESS.md)
- Strategy / frozen contracts / what doesn't port: [`00-STRATEGY.md`](00-STRATEGY.md)
- Python "before" baseline for motto #6 (re-measured at Phase 8): [`05-BASELINE.md`](05-BASELINE.md)
- Rust workspace overview + commands: [`../../rust/README.md`](../../rust/README.md)
