# 02 — Technology Mapping & Hard-Pattern Translations

## 1. Dependency-by-dependency mapping

| Python | Rust | Notes |
|---|---|---|
| FastAPI + uvicorn | **axum** (+ tower, tower-http) | Routers → `Router::nest`; DI/lifespan → an `AppState` struct + `FromRef`; middleware → tower layers. Pure-ASGI middlewares port naturally (they were written to avoid BaseHTTPMiddleware's task hop — tower has no such hazard). |
| pydantic v2 (TMF schemas) | **serde** + `#[serde(rename_all = "camelCase")]` + explicit `validate()` fns (or `garde`) | Validators become functions returning the existing `PolicyViolation`-shaped errors where they're domain rules, or 422 request errors where they're shape rules. Keep TMF camelCase at the edge, snake_case internally — same split as today. |
| pydantic-settings | **figment** (or hand-rolled envy + dotenv) | Must reproduce: repo-root `.env` loading, fail-fast validation at startup (token sentinel, autonomy mode), and the two deliberate exceptions (branding vars re-read per render). |
| SQLAlchemy 2.0 async + asyncpg | **sqlx** (postgres, tokio, rustls) | See §2.4 — sqlx over SeaORM/Diesel. `Numeric` money → `rust_decimal`. JSONB → `serde_json::Value` or typed. pgvector → `pgvector` crate (dormant column; low priority). |
| Alembic | **keep Alembic during migration; sqlx::migrate baseline at Phase 8** | Decision D2 in doc 04. |
| aio-pika | **lapin** | Topic exchange, DLX/TTL retry topology, publisher confirms; `message_id` = event uuid. The topology is declared imperatively today — ports 1:1. |
| structlog (JSON) | **tracing** + tracing-subscriber JSON | Contextvar-bound fields → tracing spans/fields. Redaction (card/NRIC/Ki/ICCID-last-4) → a custom `tracing` Layer or field-visitor; port the redaction rules as a table + tests. |
| OpenTelemetry SDK + auto-instrumentors | **opentelemetry** + tracing-opentelemetry + OTLP/HTTP exporter | No auto-instrumentors in Rust: HTTP server/client + sqlx + lapin spans are added at the platform-crate seams (bss-clients wraps reqwest; bss-db wraps pool acquire; bss-events wraps publish/consume) — which matches the repo's "instrument at the chokepoint" doctrine anyway. |
| httpx | **reqwest** | Per-method timeouts mandatory, **no automatic retries** (doctrine). Typed error mapping (404/422-POLICY_VIOLATION/5xx/timeout) reproduced in bss-clients. |
| transitions (unused) | enums + `match` | 5 hand-rolled FSMs become `enum State` + `enum Trigger` + a `transition(state, trigger) -> Result<State, InvalidTransition>` pure fn. Strictly better than today. |
| Typer | **clap** (derive) | 24 groups / ~90 leaf commands map to nested subcommand enums. |
| Rich | **comfy-table** + **owo-colors** (+ textwrap) | The 14 bss-cockpit ASCII renderers are mostly hand-built box drawing already — port as plain string builders; Rich's Panel/Table usage is shallow. |
| prompt-toolkit | **reedline** (or rustyline) | History file, autosuggest-from-history, multiline. reedline is the closer match. |
| Jinja2 | **MiniJinja** | Deliberate choice over askama: templates are runtime-loaded assets shared across two portals, and branding uses *callable* globals (`branding()` re-resolved per render — a doctrine rule). MiniJinja syntax is Jinja2-compatible; expect mechanical fixes only (filters, `tojson`). Compile-time askama would fight the hot-reload doctrine. |
| HTMX + SSE | unchanged (vendored JS) + axum SSE responses | `bss_portal_ui.sse.format_frame` → a small SSE frame builder; the detached-task turn driving in the cockpit → tokio tasks + a broadcast channel (see §2.6). |
| LangGraph / langchain-core / langchain-openai | **hand-rolled ReAct loop** over **async-openai** (pointed at OpenRouter) | See §2.5. No Rust agent framework needed — the repo already treats LangGraph as a thin loop and hand-interprets the stream. |
| openai SDK | async-openai (OpenAI-compatible base_url) | Attribution headers, temp 0.0, max_tokens cap, fixture-swap hook for the mock model. |
| qrcode + pillow | **qrcode** crate + **image**/png | Self-serve PNG data-URIs only (no ASCII QR exists — don't add one). |
| stripe SDK | **direct HTTPS via reqwest** (recommended) or async-stripe | Decision D4. Usage is narrow (tokenize/charge/checkout-session/webhook-verify); the community async-stripe crate is heavyweight and its maintenance cadence is a risk; the doctrine already hand-rolls stripe webhook *signature* verification, so hand-rolling the 5–6 API calls keeps the SAQ-A surface auditable. |
| resend SDK | reqwest (it's one POST) | Email adapters: logging / noop / smtp (lettre) / resend. |
| hmac / hashlib / secrets | **hmac** + **sha2** + **subtle** + **rand**/getrandom | Three keying schemes (middleware fixed salt, portal pepper, webhook per-provider secrets) with exact canonical payload strings; `subtle::ConstantTimeEq` everywhere `compare_digest` is used today. Golden test vectors captured from Python before porting. |
| tomllib / tomlkit | **toml** + **toml_edit** | toml_edit is a *perfect* match for the comment-preserving settings.toml round-trip requirement. |
| pyyaml + jsonpath-ng | **serde_yaml** + **serde_json_path** | Scenario engine. |
| Postgres FTS / pgvector | raw SQL via sqlx / pgvector crate | FTS queries port verbatim; embedding column stays dormant. |
| pytest + httpx ASGITransport | cargo test + `tower::ServiceExt::oneshot` + real Postgres | Same "real DB, seeded" philosophy. hypothesis → **proptest** (subscription balance math). |
| ruff / black / mypy | rustfmt + clippy (deny warnings) | mypy-strict aspirations come free. |
| uv workspace | Cargo workspace | Kills the shared-`app`-package-name / PYTHONPATH hack. |
| Playwright e2e | **stays Python** | Language-irrelevant black-box suite. |

## 2. The six hard-pattern translations

### 2.1 ContextVar ambient context → explicit context + task-locals at the edges
The single most pervasive pattern: middleware stashes actor/channel/tenant/request-id/
service-identity in ContextVars; `bss-clients` reads them on **every** outbound request;
publishers stamp them onto audit rows; workers push/pop explicitly.

Rust translation, in order of preference:
1. **Explicit `RequestCtx` struct** carried in axum request extensions and passed as a
   parameter into services/policies/repos. This is the honest port — the codebase already
   threads `AuthContext` explicitly at the worker seams, and Rust makes the implicit flows
   visible.
2. Where explicit threading would churn every signature for one distant reader (outbound
   client headers, audit stamping), use a **`tokio::task_local!` scope** set by the middleware
   layer and read only inside `bss-clients`/`bss-events` — mirroring today's "set in
   middleware, read at the two chokepoints" reality. The chat-surface per-call token override
   (`set_service_identity_token`) becomes an explicit field on the orchestrator's tool context
   instead of a ContextVar swap.
Rule: task-locals are allowed **only** inside `bss-context` and its two chokepoint readers;
everything else takes `&RequestCtx`. That rule becomes a doctrine guard.

### 2.2 Global mutable clock → `ArcSwap<ClockState>` singleton behind the same API
`bss_clock.now()` is imported everywhere and mutated by admin endpoints (freeze/advance) for
scenario determinism. The *faithful* port is a process-global `ArcSwap<ClockState>` (wall vs
frozen) with `bss_clock::now()`, `freeze/unfreeze/advance`, duration-string parsing ("30d"),
and the same `/admin-api/v1/clock` router gated by `BSS_ALLOW_ADMIN_RESET`. Do **not**
over-engineer a Clock trait injected through every constructor — the global is a deliberate
design (one line to adopt, scenario-controllable), and the grep guard ("no
`SystemTime::now()`/`Utc::now()` outside bss-clock") ports directly.

### 2.3 Import-time registration → explicit registries (compile-checked)
- Model metadata for Alembic: irrelevant post-port (sqlx has no metadata registry).
- Tool registry: replace `@register` decorator side effects with an **explicit
  `registry()` function** listing all 109 constructors (or the `inventory` crate if
  distributed registration is wanted — explicit is recommended; `validate_profiles()` becomes
  a unit test + compile-time exhaustiveness where possible).
- Fail-fast env constructors: become `Config::from_env() -> Result<_, ConfigError>` called in
  `main()` before serving — same fail-closed behavior, now typed.

### 2.4 SQLAlchemy → sqlx (why not SeaORM/Diesel)
The repo already drops to raw SQL for every correctness-critical path (SKIP LOCKED sweeps,
pool assignment, FTS, seeds) and documents an identity-map bug that ORM magic caused. sqlx
gives: raw SQL as the primary idiom, compile-time query checking against the real schema
(`sqlx prepare` in CI), explicit transactions, no identity map, native JSONB/ARRAY/UUID/
Numeric support. Repositories stay "dumb CRUD" structs holding a `PgPool`/`Transaction`.
The ~60 ORM classes become plain structs with `FromRow`. Relationship loading
(`selectinload`) becomes explicit second queries — which is what it compiles to anyway.
Cost: hand-writing column lists; mitigated by macros and by porting repo-by-repo with the
golden contract tests watching.

### 2.5 LangGraph → a ~500-line ReAct loop you own
What LangGraph provides today: assemble messages, call model with tool schemas, execute tool
calls, loop until no tool calls, stream updates. What the repo builds *around* it (and must
port carefully): tool schema generation from type hints (→ **schemars** derive on typed arg
structs per tool + docstring→description), transcript re-parsing into messages (32k cap, tool
blocks as system messages), the AgentEvent stream types, consecutive-failure and
identical-call bailouts, `wrap_destructive` + LoopState autonomy gating, per-ToolMessage
ownership trip-wire (JSON-path checks → `serde_json_path`), cost cap accounting, the
MockChatModel fixture player for e2e. Port the *guards* 1:1 with their tests; the loop itself
is the easy 20%. The two prompts and all guard regexes (chrome filter, escalation claim,
knowledge citation) port verbatim with golden transcript tests.

### 2.6 SSE turn-driving → tokio-native
The cockpit's detached-task pattern (`_INFLIGHT` dict, pump/observe, 10s heartbeats,
reconnect-as-observer) exists because a dropped browser connection must not cancel a 20–50s
LLM turn. In Rust: spawn the turn as a `tokio::task` owning the agent stream, publish
AgentEvents into a `tokio::sync::broadcast` channel keyed by session in a `DashMap`; SSE
handlers subscribe (first = creator, reconnects = observers); heartbeat via `tokio::time::
interval`. This is *simpler* than the Python because task cancellation semantics are explicit.

## 3. What gets better / worse in Rust

**Better:** FSMs as enums (illegal transitions unrepresentable); `PolicyViolation` as a typed
error enum with `IntoResponse` (the structured JSON contract enforced by the compiler);
ownership-bound `*.mine` tools can take a `CustomerId` newtype constructible *only* from the
auth context (turning doctrine guard 8 and FORBIDDEN_MINE_PARAMETERS into type errors);
compile-time SQL; single static binaries → real distroless/scratch images, trivial motto #6
compliance; no identity-map hazards; Cargo kills the PYTHONPATH isolation hack.

**Worse / more verbose:** TMF payload validation (pydantic did a lot of silent coercion —
expect edge-case diffs in the golden tests, e.g. string→int coercion, datetime parsing
leniency); dict-shaped seams must all become structs *now*, surfacing every latent shape
assumption; hot-reload config (mtime cache + last-good fallback + toml_edit round-trip) is a
few hundred lines of careful code; LLM stream handling is verbose without langchain's message
types (define our own `Message`/`ToolCall` enums); Jinja macro/filter parity needs a pass.

## 4. Doctrine-guard disposition (all ~21)

"Structural" = made impossible by construction in Rust; "grep" = re-expressed as a ripgrep
guard over Rust sources in the new `make doctrine-check`; "test" = pinned by a unit test.

| # | Guard (Python) | Rust disposition |
|---|---|---|
| 1 | check-clock (no datetime.now outside bss-clock) | **grep**: no `Utc::now\|SystemTime::now\|Instant::now` outside `crates/bss-clock` (Instant allowed for latency metrics via allowlist) |
| 2 | chat-only orchestration (portal routes don't call mutating clients) | **grep** with same route-file carve-outs |
| 3 | OTel not in services/policies layers | **structural** (OTel only linked by platform crates) + grep |
| 4 | no campaignos leakage | **grep** (unchanged) |
| 5 | renewal reads price snapshot, not catalog | **grep** + test |
| 6 | no X-BSS-Service-Identity header trust | **structural**: identity only constructible from TokenMap validation |
| 7 | no per-request token env reads | **structural**: Config loaded once in main; no `std::env` outside config crates → **grep** to enforce |
| 8 | customer_id never from request in post-login routes | **structural**: `CustomerId` newtype from session extractor only + grep |
| 9 | astream_once only in chat routes | **grep** (fn rename aside) |
| 10 | stripe fixture redaction | **grep** (unchanged patterns) |
| 11 | rating purity (no data_roaming in rating.rs) | **grep** |
| 12 | ported_out terminal | **test** + grep |
| 13 | renewal worker containment | **grep** |
| 14 | version from BSS_RELEASE only | **structural**: single `bss_models::BSS_RELEASE` const |
| 15 | knowledge tools operator-only | **test** (profile validation) |
| 16 | phases/V0_*.md not indexed | **test** on INDEXED_PATHS |
| 17 | outbox single-publisher | **structural**: lapin only a dependency of bss-events; publish fn not exported → grep for `basic_publish` |
| 18 | safe consumer (bind_consumer only) | **structural**: same containment |
| 19 | email palette from THEMES | **grep**: no hex literals in email renderer |
| 20 | brand not hardcoded in templates | **grep** (unchanged, templates carry over) |
| 21 | settings.toml writes only via bss-cockpit config | **structural**: toml_edit only in bss-cockpit + bss-branding read-only → grep |

Plus new Rust-specific guards: task-locals only in bss-context (§2.1); `unsafe` forbidden
workspace-wide (`#![forbid(unsafe_code)]`); `unwrap()/expect()` denied in non-test code via
clippy.
