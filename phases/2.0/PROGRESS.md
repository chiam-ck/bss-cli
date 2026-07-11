# Migration Progress Log

Running log of the phases/2.0 Rust port. One entry per work session. The plan
docs (`00`–`04`) are the *design*; this file is the *state*.

Branch: `2.0`. Workspace: [`../../rust/`](../../rust/).

---

## Tagging discipline (2.0)

Every phase gets an **annotated** git tag when — and only when — its exit criteria
in [`03-PHASES.md`](03-PHASES.md) are met (parity harness green on the mixed stack,
golden diffs clean, `cargo fmt` + `clippy -D warnings` + `test` green). The tag is
the "phase done" gate — consistent with the repo's "verify first, commit after /
one commit per phase minimum."

**Scheme — phase pre-releases of the final `v2.0.0`:**

| Tag | Cut / gated on |
|---|---|
| `v2.0.0-phase.0` | Foundations: 7 platform crates + CI + golden rig; hello-world conformance passes |
| `v2.0.0-phase.1` | rating cut over (pilot); per-service playbook written |
| `v2.0.0-phase.2` | mediation + provisioning-sim + som cut over |
| `v2.0.0-phase.3` | catalog + com cut over |
| `v2.0.0-phase.4` | **bilingual resting point** — all 9 services Rust; portals/orch/CLI still Python. Shippable pause point (strategy §5); re-measure motto #6 for the service plane vs [`05-BASELINE.md`](05-BASELINE.md) |
| `v2.0.0-phase.5` | orchestrator lib + knowledge + cockpit-core (no deployable cutover; fixture-parity green) |
| `v2.0.0-phase.6` | portals (self-serve, csr) cut over |
| `v2.0.0-phase.7` | CLI + REPL + scenarios cut over |
| `v2.0.0` | **final cutover** — all-Rust, Alembic retired, 14-day soak passed (Phase 8) |

SemVer ordering holds: `2.0.0-phase.0 < 2.0.0-phase.1 < … < 2.0.0-phase.7 < 2.0.0`
(numeric pre-release identifiers order numerically; any pre-release precedes the
release). The major bump to `2.0.0` marks the platform rewrite even though wire
contracts are frozen (§3) — the migration is behaviour-frozen, not API-versioned.

**Mechanics:**
- `git tag -a v2.0.0-phase.N -m "<phase>: <what cut over>; exit criteria met (<evidence>)"` — annotated so the message records the exit-criteria evidence.
- Tag the commit on `2.0` that *completes* the phase (post-merge if the phase ran on a feature branch). **Mid-phase commits are never tagged** — e.g. this scaffold commit is *not* `phase.0`; that tag waits until all seven crates + CI + the golden rig are done.
- Intra-phase service cutovers (P2 ×3, P4 ×3) are **commits, not tags** (`feat(payment): rust cutover`); the phase tag caps the set. If one service must be pinned for a prod canary, use an incrementing pre-release: `v2.0.0-phase.4.1`, `.2`, `.3`.
- The Python parity baseline stays `v1.8.1` on mainline; every 2.0 tag is `v2.0.0-*`, so they never collide.

---

## Phase 0 — Foundations — ✅ COMPLETE (tag `v2.0.0-phase.0`)

All exit criteria green against the live oracle via `cargo run -p conformance`
(2026-07-11): token-middleware conformance, an audit row the **Python** relay
publishes, a Rust-emitted trace in **Jaeger**, and golden HMAC vectors matching
the oracle. 8 platform crates + conformance harness; 84 unit tests + 5 live
checks; clippy `-D warnings` + fmt clean; CI wired.

Goal: Cargo workspace + CI + the seven platform crates against a throwaway
hello-world service (see `03-PHASES.md`).

### Done

- **Python baseline captured** → [`05-BASELINE.md`](05-BASELINE.md). The "before"
  measurement for motto #6, taken while the Python stack was live (it can't be
  reconstructed post-cutover). Headlines: **1.18 GiB** app-plane RAM (11
  containers), **6.36 s** full-stack cold start (all 11 booted together;
  per-service breakdown in the doc), **12.8 ms** p99 on `/health`, **~3.46 GB**
  nominal image sum, **109,297** LOC Python. Phase 8 re-measures the same way
  (§6 of that doc) and this is the comparison point.
- **Toolchain + scaffold.** rustup stable (1.97) with rustfmt + clippy. Cargo
  workspace at `rust/` (D7: subtree, not standalone repo — rationale in
  `rust/README.md`). Workspace lints: `unsafe_code = forbid`,
  clippy `unwrap_used`/`expect_used = warn` (promoted to deny by `-D warnings`).
- **CI from day one.** `.github/workflows/rust.yml` — fmt + clippy `-D warnings`
  + test on `2.0` pushes / PRs touching `rust/**`. (Closes the "no CI anywhere"
  gap the inventory flagged; sqlx-prepare job added when `bss-db` lands.)
- **`bss-clock`** (first crate — "everything reads it"). Faithful port of
  `packages/bss-clock`:
  - `now/freeze/unfreeze/advance/state/parse_duration/reset_for_tests`, wall &
    frozen modes. Process-global state via `ArcSwap<Inner>` with `rcu` writers
    (§2.2 of `02-TECH-MAPPING.md`) → lock-free `now()` reads.
  - `clock_admin_router()` (axum) mirrors the FastAPI router: `GET /clock/now`
    unguarded; `POST freeze|unfreeze|advance` gated on `BSS_ALLOW_ADMIN_RESET`;
    camelCase wire shape (`offsetSeconds`/`frozenAt`), RFC-3339 instants,
    `{"detail":{code,message}}` errors, 403/422 parity.
  - 15 integration tests porting `tests/test_clock.py` 1:1 (serialized on a
    process-global `Mutex` since the clock is a singleton). All green; fmt +
    clippy clean.

- **`bss-context`** — the §2.1 ContextVar translation. Unifies the Python
  per-service `auth_context.AuthContext` **and** `bss_clients.base` context vars
  into one `RequestCtx` (actor/tenant/channel/service_identity/request_id + roles/
  permissions, defaults matching the dataclass). Carried explicitly in axum
  extensions (`Extension<RequestCtx>`) *and* mirrored into a `tokio::task_local!`
  scope for the two chokepoint readers (bss-clients, bss-events) — the task-local
  lives only in this crate (future doctrine guard). `propagate_context` middleware
  ports `RequestIdMiddleware` (header→ctx, echo `x-request-id`); `service_identity`
  comes from a `ServiceIdentity` extension the token layer will set, never a header
  (guard #6 made structural). 10 tests (ports `test_auth_context.py` +
  `test_header_propagation.py` intent + task isolation); fmt + clippy clean.
  - Deferred: the `set_service_identity_token` per-call override becomes an explicit
    field on the orchestrator tool-context in P5 (§2.1), not a task-local — noted so
    bss-clients doesn't reach for one.

- **`bss-middleware`** — perimeter `X-BSS-API-Token` auth (risk R4). `TokenMap`
  (HMAC-SHA-256 via `hmac`+`sha2`, constant-time full-scan lookup via `subtle`,
  env-name→identity derivation), loader + validator (default-required, unique
  identities/tokens, sentinel/length), and the axum `require_api_token` gate
  (`/health*` + `/webhooks/` exemptions, 401 shapes). Wires to bss-context: the
  gate inserts `ServiceIdentity` (guard #6 — identity from the token only, never a
  header), the context layer reads it — proven by a composed layer test.
  - **Golden-vector conformance**: captured HMAC digests + identity derivations
    from the live Python oracle → `tests/golden_vectors.json`; two Rust tests
    assert byte-identical hashing/derivation. This is the R4 mitigation.
  - 28 tests (port `test_api_token.py` + `test_token_auth.py` + golden). Deferred:
    the per-`(remote,path)` 401 log throttle (observability; lands with bss-telemetry).

- **`bss-db`** — the `PolicyViolation` type (the single most load-bearing payload;
  the LLM reads it) + sqlx pool. Ports `policies/base.PolicyViolation` (raise side,
  field `rule`), the `RequestIdMiddleware` 422 serialization (wire side: `rule`→
  `reason` + derived `referenceError`, five keys exactly), and the client parse
  (`bss_clients.base._handle_response`) as `from_wire`. `IntoResponse` makes the
  422 contract compiler-enforced. sqlx `PgPool` with the SQLAlchemy 5+5 config
  (`connect`). 7 tests pin the exact wire shape + server→client round-trip.
  - Deferred: a live-captured golden 422 from the running stack can augment the
    hand-pinned shape once the conformance service exists.
- **`bss-models`** (started) — `BSS_RELEASE` single source of truth (guard #14),
  tracking the Python baseline `1.8.1`. The ~60 per-table `FromRow` structs are
  intentionally deferred: each ports **with its service** (P1–P4) against that
  service's golden contract tests, where the R1 dict-shape hazards concentrate.

- **`bss-clients`** (base done) — the reqwest S2S base. Ports `BSSClient`:
  mandatory per-request timeouts, **no retries**, typed `ClientError` (404→NotFound,
  422+POLICY_VIOLATION→`Policy(bss_db::PolicyViolation)` reusing that type, other
  422/4xx→Http, 5xx→Server, timeout, transport). `AuthProvider` trait +
  No/Token/Bearer/NamedToken (fail-fast constructors; NamedToken primary→fallback
  env). Context propagation reads `bss_context::current().outbound_headers()` with
  set-default semantics — **no `set_context`**, the §2.1 unification pays off. 11
  tests run the real reqwest path against a local axum peer (respx equivalent):
  error mapping, no-retry (call-count=1), per-call timeout, auth+ctx headers.
  - Deferred: the 12 typed clients (CRMClient, …) port per-phase (P1–P4); the
    per-call service-identity token override lands with the orchestrator (P5, §2.1).

- **`bss-telemetry`** (rules done) — the two pure, load-bearing pieces: the
  log-field **redaction** rules (`REDACTED_KEYS` minus `_ref`/`_id` suffixes →
  `***REDACTED***`, top-level keys only, no recursion — ports `redact_sensitive`)
  and the **semconv** span attribute keys (`bss.*`, last4-only discipline). 4 tests.
  - Deferred to the conformance-service step: the tracing-subscriber JSON setup,
    the OTLP/OTel exporter, and the tracing `Layer` that applies `redact_event` to
    live events (validated against Jaeger there) — "instrument at the chokepoint".

- **`bss-events`** (core done) — the transactional-outbox plane, broker-free core:
  - `stage_event` builds the `audit.domain_event` row stamped from `RequestCtx` +
    `bss_clock::now()` (ports `events/publisher.publish`); `published_to_mq=false`.
  - `drain_batch` — the relay orchestration (publish→mark, at-least-once, null
    payload→`{}`) over an `EventPublisher` trait; tested against a fake. The
    `DRAIN_SQL`/`MARK_OK_SQL`/`MARK_FAIL_SQL` are verbatim (SKIP LOCKED, oldest
    first). `relay_mode(None)=Off` (delivery off, log still records).
  - `decide_retry` (park at `>= max_retries`, else nack-retry) + `death_count`
    (`x-death[0].count`) — the safe-consumer decision, plus `CLAIM_INBOX_SQL`.
  - `topology` — the frozen RabbitMQ contract as assertable data (exchange names,
    main/retry queue args, parked/retry names) so a Rust and a Python service share
    a broker byte-identically during migration.
  - 8 tests (port `test_relay.py` + `test_consumer.py` intent + contract pins).
  - Deferred to conformance: lapin connect/declare/consume, the sqlx tick loop, and
    the `/audit-api/v1` read router (needs Postgres+RabbitMQ to validate).

- **`conformance` harness** (`rust/conformance`, `cargo run -p conformance`) — the
  Phase-0 exit harness, run against the **live stack** (Postgres/RabbitMQ on
  `tech-vm`, the same infra the Python services use; reachable from the dev host
  over Tailscale). Never runs in CI. **All checks green (2026-07-11):**
  - sqlx connects to the live Postgres (16.14).
  - `audit.domain_event` schema matches `bss_events::DomainEvent` (16/16 columns).
  - **cross-language outbox interop: the *Python* relay published a Rust-written
    audit row** — INSERT an inert `conformance.ping` (no consumer bound), poll until
    `published_to_mq` flips, then DELETE. Zero side effects.
  - token middleware end-to-end over real HTTP with the live `BSS_API_TOKEN`
    (health 200 / no-token 401 / valid-token 200, identity=`default`).
  - Component model confirmed for the human: sqlx/lapin/reqwest/otel are libraries
    compiled into the binary — **no new infra, nothing to deploy**; Rust reuses the
    existing Postgres/RabbitMQ/Jaeger. (D-note in `rust/README.md`.)

- **`bss-telemetry` OTel bootstrap** — `init_telemetry(service)` builds a
  `TracerProvider` with an OTLP/HTTP-protobuf exporter to the same Jaeger the
  Python stack uses (`service.name = bss-<service>`, `TraceIdRatioBased` sampler,
  batch export), bridges `tracing` spans via tracing-opentelemetry, adds a JSON
  log layer, and never panics (falls back to logs-only). `TelemetryGuard` flushes
  on drop. `emit_probe_span` returns a trace id for the Jaeger conformance check.
  opentelemetry 0.27.x pinned (R6 version-matrix resolved cleanly).
  - One follow-up: the redaction **Layer** over live `tracing` fields (the rules +
    `redact_event` exist; wiring them as a fmt field-visitor lands when the first
    service logs sensitive fields — no service does yet).

### Phase 0 done → Phase 1 (rating pilot)

Tagged `v2.0.0-phase.0`. Next: **Phase 1 — port the rating service** (smallest,
"rating is a pure function"), the pilot that turns the platform crates into a
running Rust service and produces the per-service porting playbook. This is where
the per-endpoint golden-contract capture rig gets fleshed out (capture rating's
request/response/event JSON from the Python oracle, diff the Rust service against
it), and where bss-clients' first typed client (catalog) + the lapin/sqlx service
wiring (relay tick loop, consumer, `/audit-api/v1` router) land.

### Notes / decisions taken

- **Deps pinned minimal:** chrono, arc-swap, serde_json, axum (+ tokio/tower dev).
  No `regex` — `parse_duration` is hand-rolled to match `^\s*(\d+)\s*([smhd])\s*$`
  without the dependency.
- Clock tests need `--test-threads` safety: solved in-crate with a serialising
  `Mutex` + `reset_for_tests()`, not by constraining the runner.
