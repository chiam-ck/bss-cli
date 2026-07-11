# 00 — Strategy & Thought Process

This doc records *why* the plan is shaped the way it is, so a future reader (or a future
phase) can re-derive the decisions instead of cargo-culting them.

## 1. What we're actually migrating

BSS-CLI is not one application; it is **four distinct kinds of software** sharing a monorepo:

1. **Nine stateless-ish HTTP microservices** (crm, subscription, payment, catalog, com, som,
   mediation, rating, provisioning-sim) — FastAPI apps with a strict internal layering
   (Router → Service → Policies → Repository → Event publisher), schema-per-service Postgres,
   and a transactional-outbox event plane over RabbitMQ.
2. **A shared platform layer** (15 `packages/bss-*` workspace packages) — clock, token
   middleware, typed HTTP clients, ORM models + Alembic, outbox relay + safe consumer,
   telemetry, portal auth, branding, cockpit conversation store, knowledge FTS, webhook
   signatures, seeding, admin reset.
3. **Two server-rendered HTMX portals** (customer self-serve :9001, operator cockpit :9002) —
   Jinja + SSE, no SPA, no bundler.
4. **An LLM surface** — an orchestrator (109 registered tools, two curated profiles, a ReAct
   loop with safety/ownership/cost guards) linked *in-process* by both portals and the
   Typer/Rich CLI + REPL.

Sizes (Python, excluding venvs): services ≈ 33k LOC, packages ≈ 30k (≈ 22k non-test),
portals ≈ 23k, orchestrator ≈ 11k, cli ≈ 10k. ~1,750 test functions. ~30 Alembic migrations.
Full inventory in [01-INVENTORY.md](01-INVENTORY.md).

## 2. The three options considered

### Option A — Big-bang rewrite (rejected)
Rewrite everything on a branch, cut over once. Rejected because: 109k LOC with no
compiler-checked spec of behavior; the behavioral spec lives in ~1,750 tests that are
*coupled to Python in-process apps* (httpx `ASGITransport` against the FastAPI app), so the
rewrite would run blind for months; and there is no CI system (validation is `make` targets),
so a long-lived divergent branch has no safety net at all.

### Option B — Incremental via PyO3/FFI bindings (rejected)
Port hot modules to Rust, call them from Python. Rejected because there is no performance
problem to solve module-by-module (motto #6's budgets are generous for this workload), and the
value of the port is presumably *whole-system* properties (single static binaries, type-safe
policies, memory footprint, no Python runtime). FFI would add a third language boundary and
leave every hard cross-cutting concern (ContextVars, ORM identity map, async event loop
interop) in place — the worst of both worlds.

### Option C — Strangler-fig behind frozen wire contracts (chosen)
Replace one deployable at a time, keeping every *external* contract byte-identical, with the
Python system running alongside as the oracle until each cutover. Chosen because the Python
codebase's own doctrine already paid for it:

- **"Never shared DB access across service boundaries"** + **schema-per-service** means a Rust
  crm container and a Python subscription container can coexist against the same Postgres with
  no coordination.
- **"Inter-service calls via HTTP (bss-clients) or RabbitMQ"** means the seams are network
  seams, which are language-agnostic.
- **The outbox pattern** (`audit.domain_event` staged in the domain transaction; a relay is the
  only publisher) means event semantics are defined by *rows and routing keys*, not by aio-pika
  internals.
- **The scenario suite is black-box.** `bss scenario run-all` drives tools/HTTP and asserts on
  API responses + audit events. The 19 hero scenarios are the ship gate today and become the
  cross-language acceptance harness for free.

## 3. The frozen contracts

Everything below is **contract, not implementation** — the Rust port must reproduce it
byte-for-byte (or wire-compatibly), and no phase may change it until Phase 8:

1. **Postgres schema** — all 13 schemas (`crm`, `subscription`, `catalog`, `payment`,
   `order_mgmt`, `service_inventory`, `provisioning`, `inventory`, `mediation`, `audit`,
   `portal_auth`, `integrations`, `cockpit`/`knowledge`), sequences, partial indexes, JSONB
   shapes. Alembic (Python) remains the migration authority until the last Python component is
   retired (see Decision D2).
2. **HTTP surfaces** — every TMF path + payload (camelCase per spec), every internal
   `/…-api/v1` route, `/health*`, `/audit-api/v1`, `/admin-api/v1` (reset, clock). The
   structured `PolicyViolation` error shape (`code`/`rule`/`message`/`context`) is the most
   load-bearing single payload: the LLM reads it as a tool observation.
3. **RabbitMQ topology** — durable topic exchange `bss.events`, routing keys = `event_type`,
   the retry/parked topology (`bss.events.retry`, `<q>.retry` TTL queues, `<q>.parked`),
   AMQP `message_id` = durable event id, inbox-table dedup (`<schema>.processed_event`).
4. **Auth material formats** — `X-BSS-API-Token` + `TokenMap` env-var naming convention
   (`BSS_<NAME>_API_TOKEN` → identity), HMAC-SHA-256 token hashing (middleware fixed salt;
   portal pepper as HMAC key), the three webhook signature schemes (svix / stripe /
   didit_hmac) with their exact canonical signed-payload strings, timing-safe compares.
5. **The `.env` contract** — all ~50 `BSS_*` vars keep names and semantics (including the
   deliberate exceptions: branding vars re-read per render; tokens load once at startup).
6. **Prompts and LLM-facing text** — `_COCKPIT_INVARIANTS`, the customer chat templates, the
   five escalation categories, OPERATOR.md handling, chrome-filter behavior. These are
   *behavioral contracts with a model*, ported verbatim, then re-validated with the soak corpus.
7. **ID surface** — prefixed IDs (`CUST-001`, `SUB-007`, `SES-YYYYMMDD-hex`, …) and their
   generation rules (sequences, `secrets`-grade randomness).

## 4. What deliberately does NOT port

- **The Playwright e2e suite (`packages/bss-e2e`)** stays Python. It tests browsers over HTTP;
  its language is irrelevant, and rewriting it would burn weeks re-validating the validator.
- **Alembic migrations** are not rewritten. The 30 historical migrations are frozen history;
  at cutover (Phase 8) the schema is baselined into `sqlx::migrate` (one squashed baseline +
  new migrations in Rust from then on).
- **Templates, HTMX JS, CSS, prompts, OPERATOR.md, scenario YAML, runbooks** — carried over as
  assets. Jinja templates need mechanical adjustment to MiniJinja (near-identical syntax), not
  redesign.
- **One-off root scripts** (`backfill_loyalty_customers.py`, `seed_targeted_campaign.py`) —
  historical, not ported.
- **The vestigial `transitions` dependency** — the library is declared in crm but never
  imported anywhere; FSMs are hand-rolled dicts. In Rust they become enums + `match`, which is
  strictly better.

## 5. Why this phase order (the phasing logic)

The ordering falls out of four constraints, applied in priority order:

1. **Platform before consumers.** Every service needs the same seven crates (clock, context,
   token middleware, db, events, clients, telemetry). Building them against a throwaway
   hello-world service first (Phase 0) means the pilot service validates the *pattern*, not the
   plumbing.
2. **Cheapest full-pattern proof first.** The pilot must exercise the whole shape — axum app
   factory, policy layer, outbox staging, MQ consumer, token middleware, OTel, audit router,
   admin reset, Docker, scenario pass — on the smallest body of business logic. That is
   **rating** (1.4k LOC, "rating is a pure function" by doctrine). If the pattern is wrong, we
   find out for 1.4k LOC, not 7.4k.
3. **Fan-in order for services.** Services that others call heavily (crm — which also hosts
   Inventory — catalog, payment, subscription) go *after* their callers' patterns are proven,
   because their cutover is the riskiest and benefits most from accumulated harness maturity.
   The event-plane services (mediation, provisioning-sim, som) go early because they are small
   and exercise the consumer/worker machinery hardest.
4. **In-process dependencies dictate the tail.** Portals and CLI link the orchestrator
   in-process (`astream_once` is a function call, not an HTTP hop — and a doctrine guard keeps
   it that way). So: orchestrator (as a Rust lib crate) → portals (axum bins that depend on it)
   → CLI/REPL (a bin that depends on it). Porting portals first would require a temporary
   orchestrator sidecar service — a new network hop the doctrine forbids and we'd have to
   un-ship later. Meanwhile the *Python* portals keep working against Rust services throughout
   Phases 1–4, because they talk to services over HTTP.

One consequence worth stating explicitly: **after Phase 4, the system is bilingual and stable.**
All nine services are Rust; portals/orchestrator/CLI are still Python, talking to them over
HTTP exactly as before. That is a legitimate long-lived resting point — if priorities change,
the project can pause there with a coherent system in production.

## 6. Verification strategy (how we know each phase worked)

- **Oracle runs.** The Python repo stays checked out and runnable. For each service cutover:
  run `make scenarios-hero` (and the affected non-hero scenarios) against (a) all-Python stack,
  (b) stack with the one Rust container swapped in. Diff the scenario reports and the
  `audit.domain_event` rows they generate (event types, payload shapes, actor/channel stamping).
- **Contract snapshot tests.** During Phase 0, capture golden JSON for every endpoint the
  scenario suite touches (responses + emitted events) from the Python oracle; Rust services
  must match modulo timestamps/ids. These live in the Rust repo and outlast the oracle.
- **Unit-test translation, not transliteration.** Python service tests run httpx against the
  in-process app with a real Postgres. Rust equivalents use axum's `tower::ServiceExt`
  (`oneshot`) against the router with the same real Postgres + `make seed` fixture. Property
  tests (subscription used `hypothesis` on balance math) become `proptest`.
- **Doctrine guards re-expressed.** Each of the ~21 grep guards is either (a) made structurally
  impossible in Rust (preferred — e.g. customer_id binding via a newtype that can only be
  constructed from the auth context), or (b) re-written as a ripgrep guard over Rust sources in
  `make doctrine-check`. Guard-by-guard disposition in [02-TECH-MAPPING.md](02-TECH-MAPPING.md) §4.
- **LLM behavior parity.** The orchestrator port re-runs the e2e fixture corpus
  (`BSS_LLM_FIXTURE_PATH` deterministic MockChatModel — reimplemented in Rust) and then the
  soak runner (`scenarios/soak/run_soak.py`, kept Python) against a live model before cutover.

## 7. Repo & workspace shape

New repo (this directory) becomes a **Cargo workspace mirroring the uv workspace**:

```
bss-cli-rust/
├── Cargo.toml                  # [workspace] members = ["crates/*", "services/*", "portals/*", "cli"]
├── crates/                     # ← packages/bss-* equivalents (lib crates)
│   ├── bss-clock/  bss-context/  bss-middleware/  bss-db/  bss-models/
│   ├── bss-events/ bss-clients/  bss-telemetry/   bss-branding/
│   ├── bss-portal-auth/ bss-portal-ui/ bss-webhooks/ bss-knowledge/
│   ├── bss-cockpit/ bss-seed/ bss-admin/
│   └── bss-orchestrator/       # lib crate (linked by portals + cli, like today)
├── services/                   # 9 bin crates, one per service
├── portals/                    # 2 bin crates (self-serve, csr)
├── cli/                        # bss bin (Typer/Rich/REPL equivalent)
├── migrations/                 # empty until Phase 8 baseline
├── assets/                     # templates, htmx, css, prompts (copied from Python repo)
└── docker/                     # per-bin Dockerfiles (scratch/distroless — now actually possible)
```

Cargo solves two standing Python annoyances outright: services can stop sharing the `app`
package name (today `make test` needs per-directory `PYTHONPATH` isolation), and "distroless
where practical" (a CLAUDE.md aspiration the Python Dockerfiles never achieved — they need
curl + a venv) becomes trivial with static binaries and a `FROM scratch`/distroless final stage.

## 8. Definition of done

1. All 12 deployables (9 services, 2 portals, CLI) are Rust binaries; compose runs the Rust
   images; `make demo-restore`, `make scenarios-hero`, `make e2e`, and the soak smoke pass.
2. `make doctrine-check` exists for the Rust tree with every guard either structural or grep.
3. Alembic is retired; `migrations/` holds the sqlx baseline + at least one proving migration.
4. Motto #6 re-measured and documented (expect large headroom: RAM well under 4GB, cold start
   well under 30s, p99 under 50ms).
5. Runbooks updated where commands changed; DECISIONS.md gains the migration entries.
6. The Python repo is archived (tag + README pointer), not deleted.
