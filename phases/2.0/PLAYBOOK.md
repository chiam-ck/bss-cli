# Per-Service Porting Playbook

The validated, repeatable recipe for porting one Python service to Rust. Written
against the **rating** pilot (Phase 1) and meant to be stamped 8 more times
(P2–P4). Read `00-STRATEGY.md` for the *why*; this is the *how*, step by step.

> **The prime directive: behaviour-frozen.** The port changes zero external
> behaviour until Phase 8. Same routes, same status codes, same JSON shapes, same
> event routing keys, same audit rows. The Python service stays the **oracle** —
> when in doubt, read it and match it byte-for-byte. New features go to the Python
> repo, never here (risk R5).

---

## 0. Before you touch Rust — read the oracle

Read the whole Python service, in dependency order, and write down its contracts:

1. `app/domain/` — the pure logic. This is the highest-value, most-testable part.
2. `app/api/` (routers) — every route: method, path, request shape, response
   shape, status codes, error bodies.
3. `app/events/` — consumer (which routing keys it binds, what it emits) and
   publisher (inline-publish vs the outbox relay — **check which**; only
   subscription/com/som run the relay, the rest inline-publish).
4. `app/middleware.py` — how `RatingError`-style domain errors become HTTP
   responses (the ASGI middleware catches; in Rust that becomes an `ApiError`
   `IntoResponse`).
5. `app/dependencies.py` / `main.py` — lifespan wiring, which clients it builds,
   which routers it mounts (`/health`, `/<svc>-api/v1`, `/admin-api/v1` clock,
   `/audit-api/v1` events).
6. `tests/` — the behavioural spec. The pure-function tests port 1:1 to Rust CI
   tests; the DB/MQ integration tests become the live-smoke + the hero scenario.

**Capture the live wire shapes.** Hit the running Python service and record the
exact JSON for every endpoint the scenarios touch (`curl` the live container on
its published port). This is the golden-contract rig — diff the Rust output
against it. For rating, the load-bearing catch was that live `PLAN_M` carries a
`data_roaming` allowance and `taxIncludedAmount.value` is a JSON *number* while
the currency lives in `.unit` — an R1 dict-shape hazard the pure code must read
exactly as the oracle does.

## 1. Scaffold the crate

```
rust/services/<svc>/
├── Cargo.toml           # lib + bin; workspace deps only
├── Dockerfile           # multi-stage, distroless final (copy from rating)
└── src/
    ├── lib.rs           # module decls + `create_app(state, token_map) -> Router`
    ├── main.rs          # tokio entrypoint = port of __main__ + lifespan
    ├── config.rs        # Settings::from_env() — BSS_<UPPER> vars, DB-url normalize
    ├── state.rs         # AppState { pool, <clients>, settings, mq }
    ├── error.rs         # ApiError -> IntoResponse (the middleware catches)
    ├── domain.rs        # the pure functions + #[cfg(test)] unit tests
    ├── routes.rs        # Router<AppState> factories per router group
    └── consumer.rs      # lapin consume loop + handle_* + stage_and_publish
```

Add the crate to `rust/Cargo.toml` `members` (the `services/*` glob already
covers it) and any new typed client / workspace dep.

## 2. Port the pure domain first (and its CI tests)

- Translate `app/domain/*` to `domain.rs` as pure functions over
  `serde_json::Value` (keep the dict-shape reads faithful — that's where R1
  hides) returning `Result<_, DomainError>`.
- **Factor the consumer's branching into a pure decision function**
  (rating: `decide_usage_outcome(body, tariff, offering_id) -> UsageOutcome`).
  This makes the full event-shape decision — routing key + payload — unit-testable
  in CI without infra, which is exactly what the Python `test_*_event_consumer.py`
  asserts on. The I/O glue stays thin around it.
- Port the pure-function tests 1:1 into `#[cfg(test)] mod tests` (module-level
  `#![allow(clippy::unwrap_used, clippy::expect_used)]`). Match the Python error
  *substrings* (`.contains("no 'voice' allowance")`) so the messages stay wire-stable.

## 3. Build the HTTP surface

- One `Router<AppState>` factory per Python router group. **axum 0.7 path params
  are `:name`, not `{name}`.**
- `create_app`: build the `AppState`-carrying routes, `.with_state(state)` to
  finalize them to `Router<()>`, *then* nest the stateless shared routers
  (`clock_admin_router()`, `audit_events_router(pool)`), then apply layers:
  `propagate_context` (inner) and `require_api_token` (added last = outermost).
- **Exempt paths:** only `/health`, `/health/ready`, `/health/live` — so a
  service's `/ready` **requires** a token (rating's does; this surprised us — the
  Python `/ready` is not exempt either). Match the oracle, don't "fix" it.
- Domain errors → `ApiError` `IntoResponse` reproducing the ASGI middleware's
  shapes (rating: `RatingError` → `422 {code,message}`; upstream 5xx →
  `500 {detail:"Upstream service error"}`; not-found → `404 {detail}`).

## 4. Wire events (lapin)

- `bss_events::MqChannel::connect(mq_url)` — declares the durable `bss.events`
  topic exchange, prefetch 5. **Gotcha:** an AMQP URL ending in a bare `/` is an
  *empty* vhost to lapin but the default `/` to aio-pika; `MqChannel` normalizes
  it to `%2f`. Match the broker the Python services actually use.
- Consumer: `declare_and_bind(queue, routing_key, tag)` → drive the stream, run
  `handle_*`, then **ack unconditionally** (the handler owns its errors; rating
  never requeues — mirrors `async with message.process()` swallowing exceptions).
- Publisher: inline-publish services stage the audit row **and** publish in the
  handler. In Rust: publish first, then `INSERT` the `audit.domain_event` row with
  the resolved `published_to_mq` flag — identical final DB state to the Python
  "stage → publish → set flag → commit", best-effort delivery backed by the
  durable row. The consumer has no request context, so stamp the row from
  `RequestCtx::default()` (= the Python `auth_context.current()` default:
  `system`/`system`/`DEFAULT`/`default`).

## 5. Prove it against the LIVE stack (`tests/live_smoke.rs`, `#[ignore]`)

The Phase-0 conformance discipline, per service. Everything **inert and cleaned
up** — never mutate seeded balances. Run with the stack up and `--ignored`:

1. **typed client ↔ live peer** + pure fn on real data (catches R1 drift);
2. **full HTTP stack** end-to-end (health / authed route / 401 / domain-422 /
   audit read) against live infra, via an in-process `axum::serve` on a random port;
3. **outbox INSERT + audit read-back** for an inert aggregate, then `DELETE`;
4. **consumer cutover** — the real exit shape: `docker stop` the Python
   container so the Rust consumer is the *sole* drainer of the shared durable
   queue, publish one synthetic `usage.recorded` (non-existent subscription id so
   downstream handlers no-op and ack — verify the downstream catches-and-acks
   first), assert the Rust-written `usage.rated` audit row (`published_to_mq=true`),
   clean up, and **always restart the Python container** (bash `trap ... EXIT`).

Local topology note (this dev box): the bss **app** containers run locally with
published ports (`localhost:8001` catalog … `:8008` rating); the **infra**
(Postgres/RabbitMQ/Jaeger) runs on the remote `tech-vm` over Tailscale
(`5432/5672/16686` reachable, app ports are not). Point `BSS_CATALOG_URL` at
`http://localhost:8001` for the smoke.

## 6. Green gate + hero scenario + tag

- `cargo fmt --all --check` + `cargo clippy --all-targets --all-features -- -D
  warnings` + `cargo test` (CI-safe; the live smoke is `#[ignore]`).
- **Hero scenario on the mixed stack** — the true cutover proof: build the image
  (`docker build -f services/<svc>/Dockerfile -t bss-<svc>:rust .` from `rust/`),
  swap it into compose for the Python one, run `make scenarios-hero` (rating:
  the usage-flow scenarios), confirm green, then restore. (Phase 1 proved the
  consumer path via the cutover smoke; the compose-swap image build is the P8
  container pass, but the Dockerfile lands now.)
- Update `PROGRESS.md`, tag `v2.0.0-phase.N` (annotated, exit-criteria evidence
  in the message). Intra-phase service cutovers are commits; the phase tag caps
  the set.

## What landed in the platform crates while porting rating (reuse these)

- `bss-clients::CatalogClient` — first typed client; add per-service clients the
  same way (thin wrapper over `BssClient`, only the calls the phase needs).
- `bss-events::audit_events_router(pool)` — the shared `/audit-api/v1` read router.
- `bss-events::MqChannel` — lapin connect / declare-exchange / `publish_json` /
  `declare_and_bind`. The relay tick loop is still deferred to P2 (som/com/
  subscription actually run it, so it's validated against real retry/park there).

## Sequencing checklist (copy per service)

- [ ] Read oracle: domain, routers, events, middleware, lifespan, tests
- [ ] Capture live wire shapes for scenario-touched endpoints (golden rig)
- [ ] Scaffold crate (lib+bin+Dockerfile), add to workspace
- [ ] Port pure domain + factor the consumer decision; CI unit tests 1:1
- [ ] HTTP surface + `create_app` layering + exempt-path parity
- [ ] Typed client(s) the service needs (into `bss-clients`)
- [ ] Consumer + publisher (inline vs relay — match the oracle)
- [ ] `live_smoke.rs`: client, HTTP, outbox, consumer-cutover (all inert)
- [ ] fmt + clippy `-D warnings` + test green
- [ ] Hero scenario green on mixed stack; tag `v2.0.0-phase.N`
