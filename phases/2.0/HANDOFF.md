# Session Handoff — start here on a fresh session

This is the cold-start guide for resuming the Rust migration. Read this first,
then [`PROGRESS.md`](PROGRESS.md) for the detailed log and [`00-STRATEGY.md`](00-STRATEGY.md)
for the why.

## Where we are (2026-07-15)

**Phase 4 is COMPLETE — the ENTIRE SERVICE PLANE IS RUST. Tagged `v2.0.0-phase.4`.**
All 8 backend services run Rust images. The **portals + CLI** remain Python.

**Phase 5 is COMPLETE — the LLM/lib side is Rust. Tagged `v2.0.0-phase.5`.** P5 had
**no container cutover of its own** (D3): `bss-knowledge` (P5a), `bss-cockpit` core
(P5b), and `bss-orchestrator` (P5c) are *library* crates that cut over in P6/P7 when
the Rust portals/CLI link them. **P5c is DONE** — all 110 tools + the hand-rolled
ReAct loop + guard stack + `MockChatModel` fixture player + the v0.12 ownership trip-
wire + verbatim prompts + the `OpenRouterChatModel` production client. Validated
end-to-end (a live OpenRouter turn drove the loop against the running Rust services).
Deferred to P6 (route-coupled): `chat_caps` + `ownership::record_violation` —
**both landed in P6b s14**.

**➡️ Phase 6 — the portals — 🚧 IN PROGRESS (code complete; acceptance remains).**
Self-serve 9001 + cockpit 9002 link the P5 library crates, add the CRM screens +
chat routes, and are the first acceptance target for the 4 standing hero failures.
Decomposition: **P6a** shared crates ✅ → **P6b** self-serve ✅ → **P6c**
csr/cockpit ✅ (all seven CRM screens + settings/branding/handoff + the s5a config
writers, `bf20585`) → **P6 acceptance** ⬅️ *next* (hero 19/19 + the brand-aware
assertion). See `03-PHASES.md` §Phase 6 + PROGRESS §Phase 6.

- **P6a — the shared crates — ✅ DONE (all 4 slices).**
  - `bss-branding` (read path + THEMES + marks + css + assets + logo helper;
    phosphor-block doctrine pin).
  - `bss-portal-auth` (security foundation: OTP + magic-link login, server-side
    sessions + rotation, HMAC-SHA-256 token storage w/ pepper, `select_adapter`
    email dispatch). Extended through P6b as the funnel needed it: `audit.rs`
    (`record_portal_action`), `link_to_customer`, step-up auth (`start`/`verify`/
    `consume` + per-session cap), `pending_action.rs` (stash/replay), and
    `email_change.rs` (the cross-schema `crm`+`portal_auth` atomic email change,
    the documented doctrine exception).
  - `bss-portal-ui` (chat HTML + SSE partials) + `bss-cockpit` postprocess.
  - `bss-webhooks` (signature verify svix/stripe/didit_hmac + redaction +
    idempotency) — confirmed parity; used by the prod-only webhook receivers.

- **P6b — self-serve (9001) — ✅ FEATURE-COMPLETE (s1–s14).** The entire
  customer-facing surface is ported and route-smoked:
  - **s1–s4:** app skeleton + public surface, `/plans` (first catalog read),
    session middleware + DB session layer + security allowlist, auth/login
    (OTP + magic-link) end-to-end.
  - **s5–s9:** the signup funnel — create-customer + form, KYC step (prebaked
    adapter, golden-pinned; `attest_kyc_full` wire-body fidelity), COF (mock) +
    order + poll, dashboard + eSIM PNG QR + picker/confirmation/activation.
  - **s10–s13:** profile (contact details + cross-schema email change),
    payment-methods (list/add/remove/set-default, mock), subscription writes
    (plan-change/cancel/top-up, all step-up-gated) + billing history & eSIM reads,
    `GET /api/session/:session_id` (scenario-runner poll surface).
  - **s14 (a–e): chat SSE** — `chat_caps` (fail-closed; pure `decide()`; injected
    pool), `astream_once_to` (the streaming form P5c deferred; sink returns `false`
    = consumer gone), the conversation/turn stores (`transcript_text` pinned **by
    SHA-256** — it lands in `crm.case.chat_transcript_hash`), the **ownership
    trip-wire finally wired into the loop** (it was exported but never called —
    the Rust chat had *no* output-ownership enforcement), and the 5 routes.
    Live-smoked: a real OpenRouter turn streamed `live` → tool pill → bubble →
    `done`, and cost accounting wrote an `audit.chat_usage` row **alongside rows
    the Python portal wrote** — same table, same shape.
  - **Reusable sensitive-write pattern** established: `RawForm` → `parse_form` →
    `require_linked_customer` → `check_step_up` → ownership check → one bss-clients
    write → `audit` → redirect/re-render (helpers `pub(crate)` in `profile.rs`).
  - **One piece remains before the P6b tag:** the **webhooks** (`/webhooks/resend`,
    `/webhooks/didit`) — **prod-only**, deferred throughout (sandbox runs
    logging-email + prebaked-KYC), never on the hero path. Signature verification
    is ready in `bss-webhooks`; they land with their DB stores when the prod
    providers do.
  - **⚠️ The live stack's LLM model is in no rate table.** `gemma-4-31b-it` is in
    neither `MODEL_RATES_USD_PER_M_TOK` nor the configured-model fallback, so chat
    cap accounting always takes the conservative `FALLBACK_RATE` ceiling. **Python
    does the identical thing** — an oracle config observation, not a port bug, and
    not ours to change under the behaviour freeze.
  - **⚠️ axum is 0.7, NOT 0.8** — path params are `:param`, not `{param}`. Registering
    `/signup/{plan_id}` made the whole funnel 404; only the live smoke caught it (unit
    tests can't see route-registration syntax). Live-smoke every new route.

- **⚠️ "branding text" hero failure is a STALE ASSERTION, not a bug.** The scenario
  `portal_self_serve_signup_direct.yaml` *visit /welcome* pins `"bss-cli self-serve"`,
  but post-v1.8 `/welcome` renders `{{ branding().brand_name }} self-serve` and the
  brand name is operator-configurable; the tech-vm stack runs a custom brand, so it
  fails identically on Python + Rust. Fix at acceptance = make the assertion brand-aware,
  not change portal behaviour. (Confirmed by the human.)
- Remaining 2 standing failures: `/auth/check-email` 400, Jaeger `spanCount`.

**Next:** **P6c** (cockpit 9002 + CRM screens), then **P6 acceptance** (hero 19/19,
incl. making the branding assertion brand-aware). P6b's only remainder is the
prod-only webhooks.

<details><summary>P5c slice history (1–16) — all ✅</summary>

**Slices 1–14 done — ENTIRE OPERATOR SURFACE (reads + writes) ported:**
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
- **Slice 8** — **trace + knowledge** (5 tools; the reads complete here). New
  `JaegerClient` (plain reqwest, outside the token perimeter) + `AuditClient`
  (BssClient-based, envelope-unwrapping `list_events`); ported `_summarize_trace` +
  `_latest_trace_id`; JAEGER_ERROR / NO_TRACE_RECORDED sentinels. knowledge.search/get
  back onto the ported `bss-knowledge` crate via a `sqlx::PgPool` (caller-gated on
  `BSS_KNOWLEDGE_ENABLED`); NOT_FOUND message byte-pinned by a unit test. Both
  operator-only. Live smoke green.

- **Slice 9** — **customer + interaction WRITES** (7 tools; writes begin). Six new
  `CrmClient` write methods (+ `chrono`); `attest_kyc` ports the full stub-default
  body. **⚠️ Found an owed oracle fix:** `customer.add_contact_medium` 422s on both
  Python and Rust (client sends `characteristic`, service wants top-level `value`) —
  a pre-existing Python bug; the port reproduces it faithfully (R5). Mutating live
  smoke green (create→attest→verified→update→log→close).

- **Slice 10** — **case + ticket writes** (11 tools). 11 new `CrmClient` write
  methods; FSM transitions map target-state → `{"trigger"}` in the tool layer (unknown
  → a `ValueError` observation matching Python; `ticket` `in_progress` costs a
  `get_ticket` read). `case.close`/`ticket.cancel` destructive. Mutating live smoke
  green (case + ticket lifecycle, trigger bodies accepted).

- **Slice 11** — **subscription writes** (7 tools). 7 new `SubscriptionClient` write
  methods; `terminate_with_reason` reproduces the no-body-when-default logic exactly.
  `migrate_to_new_price` LLM-hidden. Conservative live smoke green (reversible
  schedule→cancel round-trip + bogus-id error paths for the charging/destructive ones).

- **Slice 12** — **order + payment writes** (5 tools). `order.create` create+submit
  composite; `payment.add_card` runs the pure `local_tokenize_card` (unit-tested).
  New `ComClient` create/submit/cancel + `PaymentClient` create_payment_method/
  remove_method. Conservative live smoke green (real add_card + remove cleanup; bogus-
  offering sync error for create so no line is provisioned).

- **Slice 13** — **operational writes** (inventory/port_request/provisioning, 7
  tools). `provisioning.set_fault_injection` is a list→find→patch composite (NOT_FOUND
  sentinel). Operator-only. Live smoke green (all error/sentinel paths, no seed
  mutation).

- **Slice 14** — the **last writes** (promo + catalog admin + usage.simulate, 6
  tools). `CatalogClient` create_promotion/assign_promotion/admin_*; `MediationClient`
  submit_usage. catalog admin + usage.simulate are LLM-hidden. Operator surface
  complete. Live smoke green (error paths only).

- **Slice 15** — the **`customer_self_serve` `*.mine` wrappers** (14). `tools/mine.rs`
  — auth binding (`require_actor` → `_NoActorBound`), `assert_subscription_owned` →
  `_NotOwnedByActor`, `annotate_pricing` (rust_decimal), transcript SHA-256 for
  `case.open_for_me`. Capstone `validate_profiles`-equivalent test.
- **Slice 16** — the **finale**: `ownership.rs` (trip-wire), `prompts.rs`
  (`SYSTEM_PROMPT` + customer-chat verbatim + the ITERATIVE FLOW guard), `llm.rs`
  (`OpenRouterChatModel`, reqwest direct). End-to-end live turn validated.

**D5 (schemars) status:** the `OpenRouterChatModel` sends a permissive
`{"type":"object"}` parameter schema per tool + the byte-identical description; strict
per-tool JSON Schemas remain a documented refinement (the R2 gate runs on
`MockChatModel`, and the live turn confirms real tool-calls work with the permissive
schema).

</details>

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

## What to do next: P6 acceptance

Phases 4 and 5 are done. **P6a (shared crates), P6b self-serve, and P6c csr/cockpit
are all done.** Only the prod-only webhooks remain deferred (not on the hero path).
See [`03-PHASES.md`](03-PHASES.md) §Phase 6 and PROGRESS §Phase 6.

- **P6c — csr/cockpit (9002) — ✅ DONE (`bf20585`).** The renderer family (10
  modules, 45 byte-golden cases — the P5b debt paid), `cockpit.py` end to end, all
  seven CRM screens (customers / cases+case / orders / catalog / subscriptions /
  search), settings + branding + handoff, and the s5a `bss_cockpit` config writers.
  The v1.6.1 two-step confirm is pinned in both directions across **all ten** of the
  oracle's `_CONFIRM_GATED` entries (`rust/portals/csr/tests/routes_crm.rs`). Full
  workspace: clippy clean, 117 test groups green.
  **Client gaps closed while porting** (all real, not artefacts): paged
  `list_customers`/`list_cases`/`list_orders`; the absent `transition_case` /
  `update_case_priority` / `list_promotions` / `admin_retire_offering`; the ticket
  FSM maps moved onto `CrmClient`; the `mediation` field on `CockpitClients`.
  **Deferred to P7 on purpose:** `trace.*` / `knowledge.*` are absent from the
  cockpit's tool registry — they need a Jaeger/Audit/PgPool handle the portal
  bundle doesn't carry, and land with the CLI wiring where the registry is built
  once and shared. Noted at the call site.
- **P6 acceptance** — hero suite to **19/19**: close the 3 real standing failures
  (`/auth/check-email` 400, Jaeger `spanCount`, and whatever the portal port surfaces)
  and make the **branding-text assertion brand-aware** (it's a stale string, not a
  bug — see the ⚠️ above and PROGRESS §Phase 6).

Reference for the remaining portal work — copy from the P6b account/signup slices
already landed (`rust/portals/self-serve/src/{signup,profile,account_writes}.rs`):
the `RawForm`→`parse_form`→gate→ownership→one-write→`audit` sensitive-write pattern,
`deps.rs` self-gating (Rust `session_layer` only *resolves* the cookie; each route
self-gates, unlike Python's middleware), and MiniJinja rendering the existing Jinja
templates in place via the two-directory `path_loader`.

Older P5 recipe (kept for reference — both crates are ported):

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
