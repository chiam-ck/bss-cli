# 03 — Phase Plan

Nine phases. Each service/portal cutover means: build the Rust image, swap it into
docker-compose, run the parity harness (hero scenarios + affected non-hero scenarios +
golden contract diffs against the Python oracle), then retire the Python container from
compose (code stays in the archived repo). Sizing = person-weeks (pw) for one experienced
Rust engineer who knows the domain; ±50% honesty applies (see doc 04 §3).

The dependency graph, at a glance:

```
P0 platform crates ─→ P1 rating (pilot) ─→ P2 mediation/prov-sim/som ─→ P3 catalog/com
                                                                            │
                              ┌─────────────────────────────────────────────┘
                              ▼
                     P4 payment → subscription → crm          (all services now Rust)
                              │
                              ▼
                     P5 orchestrator lib (+ knowledge, cockpit-core)
                              │
                              ├─→ P6 portals (self-serve, then csr)
                              └─→ P7 cli + REPL + scenario engine + seed
                                            │
                                            ▼
                                   P8 cutover & decommission
```

---

## Phase 0 — Foundations (no cutover) — **4–6 pw**

**Scope:** Cargo workspace scaffold; CI from day one (GitHub Actions: fmt, clippy -D
warnings, test, `sqlx prepare` check — fixing the "no CI" gap rather than porting it); the
seven platform crates built against a throwaway hello-world service:

- `bss-clock` (ArcSwap state, duration parsing, admin router) — port first, everything reads it.
- `bss-context` (RequestCtx, task-local scope, header extraction `x-request-id`/`x-bss-actor`/
  `x-bss-channel`/`x-bss-tenant`).
- `bss-middleware` (TokenMap: HMAC-hashed entries, constant-time full-scan lookup, env-name
  convention, sentinel/length validation, `/health*` + `/webhooks/` exemptions, 401 throttle).
- `bss-db` (PgPool config 5+5, `PolicyViolation` error enum + IntoResponse, Tenant/Timestamp
  helpers, tx helpers).
- `bss-models` (plain structs + FromRow for the ~60 tables, `BSS_RELEASE`; **no migrations** —
  Alembic still owns the schema).
- `bss-events` (relay: SKIP LOCKED drain → lapin publish → mark, off-mode when MQ unset;
  bind_consumer: retry/parked topology + inbox dedup; audit read router).
- `bss-clients` (reqwest base client: mandatory timeouts, no retries, typed errors incl.
  PolicyViolationFromServer; AuthProvider trait: NoAuth/Token/Bearer/NamedToken; ctx header
  propagation; the 12 typed clients ported lazily — each lands in the phase that needs it).
- `bss-telemetry` (tracing + OTel OTLP/HTTP, JSON logs, the redaction layer with ported rules,
  never-fails-startup).

**Also:** the golden-contract capture rig (record request/response/event JSON from the Python
oracle for every scenario-touched endpoint); `_template` service conventions doc; Dockerfile
template (distroless).

**Exit criteria:** hello-world axum service passes token-middleware conformance tests (same
401/exemption behavior as Python, verified with captured vectors), emits audit rows the
Python relay happily publishes, traces land in Jaeger, hashes match Python golden vectors for
all three HMAC schemes.

## Phase 1 — Pilot: rating — **3–4 pw**

Smallest service (1.4k LOC), doctrine-pure ("rating is a pure function over JSON tariff"),
but exercises the full pattern: HTTP surface, catalog client, event consume/produce, policy
layer, audit router, admin reset plan, seed-independent.

**Exit criteria:** Rust rating container swapped into compose; `make scenarios-hero` (esp.
usage-flow scenarios) green; golden diffs clean; `tests/integration/phase_08` (mediation→
rating→subscription) green against mixed stack; a written retro adjusting the pattern before
it's stamped 8 more times. **This phase's real deliverable is the validated per-service
porting playbook.**

## Phase 2 — Event-plane services: mediation, provisioning-sim, som — **4–6 pw**

Small (1.5–1.8k each), consumer/worker-heavy: mediation's block-at-edge synchronous rating
path + roaming indicator purity (guard 11); provisioning-sim's fault injection + stuck state
+ domain worker; som's atomic MSISDN+eSIM reservation (calls crm-hosted Inventory) and
CFS/RFS decomposition. Can be split across two engineers once the P1 playbook exists.

**Exit criteria:** provisioning-retry-resilience and inventory hero scenarios green on mixed
stack; parked/retry queue behavior verified by killing handlers mid-run.

## Phase 3 — catalog + com — **5–7 pw**

catalog (4.7k): TMF620 read surface + admin writes + promotions + price/window logic; the
fattest client consumer surface, so its golden tests protect everyone downstream. com (2.8k):
ProductOrder FSM, decomposition hand-off to som, price snapshot at order time (guard 5's
producer side).

**Exit criteria:** catalog-versioning/plan-change and order hero scenarios green; Python
portals still fully functional against a now-mostly-Rust service plane (they don't know).

## Phase 4 — The big three: payment → subscription → crm — **12–16 pw**

Ordered by blast-radius growth; each is its own mini-project with its own cutover.

- **payment (5.4k, ~3–4 pw):** Stripe via direct reqwest (Decision D4), tokenizer trait with
  constructor injection (guard: no direct mock charge), idempotency-key rules (crash-retry
  same key, user-retry fresh), webhook reconciliation (dedupe on (provider,event_id),
  last-write-wins per intent, drift events), SAQ-A startup template scan moves to the portal
  phase but the server-side `tokenize`-raises rule ports now, dispute record-only.
- **subscription (6.2k, ~4–6 pw):** the renewal worker (tick loop, three sweeps,
  mark-before-dispatch, SKIP LOCKED), balance decrement under FOR UPDATE, price-snapshot
  renewal, plan-change pivot via pending fields (never terminate-and-recreate), block-on-
  exhaust semantics, VAS, proptest port of the hypothesis balance suite. Highest
  correctness stakes in the repo — double-billing and quota math live here.
- **crm (7.4k, ~5–6 pw):** 12 routers incl. Inventory pools (FOR UPDATE assignment, ported_out
  terminal quarantine), 4 FSMs, Case/Ticket invariants (close-requires-resolved-tickets),
  KYC attestation verification (webhook corroboration trust anchor, PII reduction at
  boundary), port-request aggregate, chat-transcript store.

**Exit criteria:** **all 19 hero scenarios green with an all-Rust service plane and all-Python
portals/orchestrator/CLI.** Soak smoke (`--customers 2 --days 1`) green. This is the
bilingual resting point — announce it, tag it, measure motto #6 here for the service plane.

## Phase 5 — Orchestrator lib (+ knowledge + cockpit-core) — **8–10 pw**

The hard port, done as a *library crate* with no deployable cutover of its own (its cutover
happens when portals/CLI ship in P6/P7 — Python portals keep using the Python orchestrator
until then, both against the same Rust services).

- ReAct loop over async-openai/OpenRouter; typed tool arg structs + schemars (109 tools —
  bulk but mechanical: each wraps a bss-clients call; port profile-by-profile with
  customer_self_serve first since it's smaller and ownership-critical).
- The guard stack 1:1 with tests: wrap_destructive + LoopState autonomy, failure/identical-
  call bailouts, ownership trip-wire (OWNERSHIP_PATHS), chat caps (hourly window + DB monthly
  cost, fail-closed), transcript re-parse, AgentEvent stream, MockChatModel fixture player.
- `bss-knowledge` (chunker/indexer/FTS — needed by knowledge tools) and `bss-cockpit` core
  (Conversation store, pending_destructive, chrome filter, prompt composition, settings.toml
  hot-reload with toml_edit) land here because orchestrator + both P6/P7 consumers need them.
- Prompts ported verbatim; golden transcript tests against recorded fixture runs.

**Exit criteria:** fixture-driven e2e corpus replays produce event-identical transcripts vs
Python; live-model spot runs of the soak corpus reviewed by the human (LLM behavior parity is
judgment, not diffing — see Risk R2).

## Phase 6 — Portals: self-serve, then csr — **10–14 pw**

- Shared first: `bss-portal-auth` (OTP/magic-link/session/step-up with exact HMAC-pepper
  semantics — golden vectors; rate limits; email adapters), `bss-branding` (mtime cache,
  last-good, THEMES), `bss-portal-ui` (MiniJinja env, SSE frames, chat HTML), `bss-webhooks`
  (already vector-tested in P0).
- **self-serve (~6–8 pw):** 65 endpoints; session middleware as tower layer; step-up
  stash-and-replay; signup/KYC funnel (didit + prebaked adapters); Stripe checkout + PCI
  template scan at boot; SSE chat on the Rust orchestrator; QR PNG; public-route allowlist +
  open-redirect defence; template port pass (40 + 5 partials).
- **csr (~4–6 pw):** CRM screens with section-degrading reads and the `field()` camel/snake
  dual-family reader (now a typed enum per API family — this is where dict-shape hazards
  concentrate); two-step confirm both mechanisms; cockpit SSE (broadcast-channel turn
  driving); handoff; branding screens.

**Exit criteria:** `make e2e` (Playwright, kept Python, fixture LLM) green against Rust
portals; portal hero scenarios green; step-up label cross-check test ported both directions.

## Phase 7 — CLI + REPL + scenario engine + seed/admin — **6–9 pw**

- clap tree (~90 commands, mostly thin client calls — mechanical), `bss ask`, REPL on
  reedline with slash commands + Rich-equivalent rendering via the bss-cockpit renderers,
  in-process orchestrator linkage (same as today: no network hop).
- Scenario engine (YAML runner, jsonpath assertions, freeze-clock setup, LLM steps) — port
  faithfully; it is the acceptance harness's driver, so it ports *against* recorded runs of
  the Python runner on identical scenario files.
- `bss-seed` (raw SQL, idempotent — trivial) and `bss-admin` reset router (landed with
  services, CLI wiring here).

**Exit criteria:** `bss scenario run-all scenarios --tag hero` executed by the *Rust* runner
matches the Python runner's report on the all-Rust stack; REPL soak session transcript
reviewed; `make demo-restore` fully Rust-driven.

## Phase 8 — Cutover & decommission — **3–5 pw**

- Freeze Alembic; capture `pg_dump --schema-only` as the sqlx::migrate baseline; document the
  fresh-install vs existing-install paths; retire greenlet/alembic from the runtime story.
- Rust `make doctrine-check` finalized (all 21 dispositions from doc 02 §4 + the new Rust
  guards); `make test/lint/fmt/seed/scenarios*/e2e` targets fully re-pointed.
- Distroless/scratch images, healthchecks without curl (compose `healthcheck` via the binary's
  own `--healthcheck` flag or a static probe), compose cleanup, port map unchanged (8009
  still reserved).
- Motto #6 measurement report (RAM/cold-start/p99) — expected headroom becomes the headline.
- Runbook pass (23 runbooks — update commands, keep content); DECISIONS.md entries; archive
  the Python repo with a pointer README.
- 14-day soak on the all-Rust stack before calling it done.

---

## Parallelization notes

- One engineer through P0+P1 (the playbook must have one author). From P2 onward a second
  engineer roughly halves wall-clock on P2–P4 and P6.
- P5 (orchestrator) can start during late P4 — it depends on platform crates + clients, not on
  crm being finished.
- The Python oracle must stay runnable until P8; budget ~½ day per phase for keeping the
  parity rig healthy (it is the schedule's real insurance policy).

## Sizing summary

| Phase | Scope | pw |
|---|---|---|
| 0 | Platform crates + CI + golden rig | 4–6 |
| 1 | rating pilot + playbook | 3–4 |
| 2 | mediation, provisioning-sim, som | 4–6 |
| 3 | catalog, com | 5–7 |
| 4 | payment, subscription, crm | 12–16 |
| 5 | orchestrator + knowledge + cockpit-core | 8–10 |
| 6 | portals (self-serve, csr) | 10–14 |
| 7 | CLI + REPL + scenarios + seed | 6–9 |
| 8 | cutover, migrations, doctrine, docs | 3–5 |
| **Total** | | **55–77 pw** |
