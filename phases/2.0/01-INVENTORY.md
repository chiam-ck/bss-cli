# 01 — Codebase Inventory (what the port must reproduce)

Snapshot of `~/repo/bss-cli` at commit `781b66a` (v1.8.0 era), 2026-07-07.
~109k LOC Python across 819 files. All paths below are relative to that repo.

## 1. Services (`services/`, ≈ 33k LOC)

| Service | LOC | Port | Purpose | Distinctive load |
|---|---|---|---|---|
| crm | 7,378 | 8002 | TMF629 Customer, TMF683 Interaction, TMF621 Ticket, custom Case/KYC/Agent/port-request (MNP), **plus Inventory** (MSISDN + eSIM pools) | Largest; 4 FSMs; `FOR UPDATE` on pool assignment; 12 routers |
| subscription | 6,187 | 8006 | Lifecycle, bundle balances, VAS, plan change, usage decrement, renewal | Renewal worker; `FOR UPDATE SKIP LOCKED` sweeps; price-snapshot doctrine; hypothesis-tested balance math |
| payment | 5,399 | 8003 | TMF676/666, card-on-file, Stripe tokenizer | Stripe SDK; webhook reconciliation; idempotency keys; PCI SAQ-A rules |
| catalog | 4,684 | 8001 | TMF620 offerings/specs/prices, VAS, promotions | Read-heavy; admin write paths; package named `bss_catalog` (not `app`) |
| com | 2,770 | 8004 | TMF622 ProductOrder | Orchestrates crm/catalog/payment/som/subscription via clients + events |
| som | 1,808 | 8005 | TMF641/638 ServiceOrder, CFS/RFS decomposition | Atomic MSISDN+eSIM reservation; event consumer |
| provisioning-sim | 1,587 | 8010 | Simulated HLR/PCRF/OCS/SM-DP+ | Fault injection; stuck-state; domain worker |
| mediation | 1,514 | 8007 | TMF635 online mediation (one event at a time, block-at-edge) | Event producer; roaming indicator pass-through |
| rating | 1,442 | 8008 | Rating = pure function over JSON tariff | Smallest; **pilot candidate** |
| _template | 130 | — | Scaffold | Port as a `cargo generate`-style template or just conventions |

Port 8009 is reserved (deferred billing service) — keep reserving it.

**Uniform internal layering** (the shape every Rust service must reproduce):
`main.py` app factory → pure-ASGI `RequestIdMiddleware` (inner) + `BSSApiTokenMiddleware`
(outer) → routers → services → policies → repositories; `dependencies.py` lifespan wires
telemetry, sqlalchemy engine (pool 5+5), bss-clients with `TokenAuthProvider`, MQ consumer,
outbox relay, and (subscription) the renewal worker task. `auth_context.py` = ContextVar
holding actor/tenant/roles/channel/service_identity; workers use explicit push/pop tokens.

**FSMs:** hand-rolled pure modules (STATES / TRANSITIONS dicts / TERMINAL frozenset +
`is_valid_transition` / `get_next_state`), **no callbacks, no side effects** — 5 total:
subscription (9 transitions), case (11), ticket (13), esim (10), port_request (7).
The `transitions` PyPI dep is declared in crm but never imported → do not port it.

**Postgres specifics in live use:** `FOR UPDATE SKIP LOCKED` (renewal/reminder sweeps),
`SELECT … FOR UPDATE` + `populate_existing` (balance decrement, eSIM/MSISDN pools), JSONB
everywhere, ARRAY, UUID, schema-qualified sequences (`nextval('subscription.subscription_id_seq')`),
`= ANY(:ids)`, `make_interval`, partial unique index (migration 0028), pgvector extension +
dormant `embedding vector(1024)` HNSW partial index (migration 0022), Postgres FTS
(`tsvector`/`ts_rank`/`ts_headline`) as the live knowledge search path.

**Events (transactional outbox):** `events/publisher.py::publish(...)` only *stages* an
`audit.domain_event` row (`published_to_mq=false`) in the domain transaction; the
`bss-events` relay (250ms tick, batch 100, SKIP LOCKED) is the **only** caller of
`exchange.publish` (doctrine guard 17). Consumers use `bind_consumer` (retry/parked topology,
inbox-table dedup in the handler's transaction). Every service mounts a read-only
`/audit-api/v1` router.

**Renewal worker** (`services/subscription/app/workers/renewal.py`): in-process tick loop
(default 60s), three sweeps (due / skipped / upcoming-reminder), mark-before-dispatch in txn 1
then per-id `service.renew()` in fresh sessions, `PolicyViolation` caught per-id. Wall-clock
sleep for cadence but `bss_clock.now()` in WHERE clauses (frozen-clock determinism). Admin
escape hatch `/admin-api/v1/renewal/tick-now`.

## 2. Shared packages (`packages/`, ≈ 30k LOC, ≈ 22k non-test)

Dependency layering (this is the Phase 0/5 build order):

- **Leaf:** bss-models (5.5k — 60 ORM classes, 13 schema modules, 30 Alembic migrations,
  `BSS_RELEASE` version constant), bss-clock (295 — process-global wall/frozen clock +
  `/admin-api/v1/clock` router), bss-telemetry (466 — OTel bootstrap, pinned 1.39/0.60b0,
  never-raises, lazy imports), bss-middleware (623 — `TokenMap` HMAC-hashed tokens,
  constant-time full-scan lookup, env-name→identity convention, pure-ASGI middleware,
  `/health*` + `/webhooks/` exempt), bss-branding (settings.toml `[branding]` reader,
  `THEMES` palette source of truth, mtime cache).
- **Mid:** bss-clients (3.0k — 12 typed httpx clients, no-retry doctrine, `AuthProvider`
  protocol, ContextVar actor/channel/request-id propagation + per-context token override),
  bss-events (518 — relay + bind_consumer + audit router), bss-knowledge (636 — heading
  chunker, idempotent indexer with allowlist, FTS search), bss-webhooks (682 — three
  hand-rolled HMAC schemes with skew windows + provider-keyed redaction), bss-admin (189 —
  schema-scoped reset router).
- **Upper:** bss-portal-auth (2.7k — OTP/magic-link/session/step-up, HMAC-pepper storage,
  timing-safe verify, rate limits, email adapters incl. resend), bss-cockpit (3.6k —
  Conversation store in `cockpit` schema, pending_destructive, chrome filter regexes,
  `build_cockpit_prompt` + `_COCKPIT_INVARIANTS`, 14 ASCII renderers, settings.toml/OPERATOR.md
  mtime hot-reload with last-good fallback, tomlkit comment-preserving writes), bss-seed
  (863 — 1000 MSISDNs, 1000 eSIMs, 3 plans, 4 VAS, demo data; raw SQL, idempotent).
- **Top:** bss-portal-ui (623 — Jinja/HTMX shared widgets, SSE frame formatting, chat HTML;
  depends on bss-orchestrator), bss-e2e (934 — Playwright harness; **stays Python**).

## 3. Portals (`portals/`, ≈ 23k LOC)

**self-serve** (8.7k LOC, 40 templates + 5 partials, 18 routers ≈ 65 endpoints): passwordless
auth (OTP/magic link), signup/KYC funnel (didit + prebaked adapters, Stripe checkout,
PCI template scan at boot), post-login writes **direct via bss-clients** (top-up, payment
methods, esim, cancel, profile, billing, plan change), step-up auth
(`SENSITIVE_ACTION_LABELS`, 11 labels, stash-and-replay of the original POST), chat as the
*only* orchestrator route (SSE, escalation-hallucination guard, ownership violation → generic
reply), webhooks (resend/didit) signature-verified in-handler, QR as base64 PNG data-URIs.

**csr / cockpit** (3.7k LOC, 17 templates, 12 routers ≈ 55 endpoints): no login (perimeter
doctrine), CRM screens (customers 360 with section-degrading reads, subscriptions, orders,
cases, catalog admin, search, settings/branding), two-step destructive confirm (`confirm=yes`
danger panels), chat cockpit (SSE with detached-task turn driving + observer reconnect,
anti-mimicry/recap/citation guards, canonical Proposed/Executed bubble rewrites), handoff
(`POST /cockpit/handoff` — prefilled draft, never auto-sent). Shares the Postgres
`ConversationStore` with the REPL.

## 4. Orchestrator (`orchestrator/`, ≈ 7.2k non-test LOC) — the hard port

- **LangGraph surface is thin:** one `create_react_agent(model, tools, prompt)`;
  `StructuredTool.from_function(coroutine=…)` with schemas inferred from `Annotated` type
  hints (257 LOC of semantic types) + docstrings; streaming via `astream(stream_mode="updates")`
  walked by hand. No StateGraph, no checkpointer, no multi-agent.
- **Tool registry:** `@register("dotted.name")` decorator → module-level dict; importing
  `tools/__init__` registers **109 tools** (customer 13, subscription 11, ops 9, catalog 9,
  case 8, ticket 8, inventory 6, payment 6, order 5, port_request 5, provisioning 5, som 4,
  promo 3, usage 2, mine_wrappers 14); 4 LLM-hidden.
- **Profiles:** `customer_self_serve` (~24: public reads + ownership-bound `*.mine`/`*_for_me`
  wrappers binding customer_id from `auth_context.current().actor`; `FORBIDDEN_MINE_PARAMETERS`)
  and `operator_cockpit` (~90: full registry minus mine wrappers). `validate_profiles()`
  fail-fast at import.
- **Safety:** `DESTRUCTIVE_TOOLS` (12) short-circuited by `wrap_destructive` into a structured
  `DESTRUCTIVE_OPERATION_BLOCKED` observation → propose-then-`/confirm` via
  `cockpit.pending_destructive` rows. Autonomy modes `granular`/`batched` via per-graph
  `LoopState`; `BSS_REPL_LLM_AUTONOMY` fail-closed at boot.
- **session.py (755 LOC) is the risk center:** typed AgentEvent stream; consecutive-failure
  bailout (3), identical-call bailout (3), per-ToolMessage ownership trip-wire
  (`OWNERSHIP_PATHS` JSON-path map → CRM audit row + stream kill), service-identity token
  override, transcript re-parse for multi-turn (32k char cap, tool blocks as SystemMessage).
- **Cost caps** (`chat_caps.py`, 373 LOC): in-memory hourly window + DB-backed monthly cost
  (`audit.chat_usage`), per-model USD/Mtok rate table, fail-closed.
- **LLM gateway:** ChatOpenAI → OpenRouter (`deepseek/deepseek-v4-pro`), temp 0.0, attribution
  headers; `BSS_LLM_FIXTURE_PATH` swaps a deterministic MockChatModel for e2e.
- **Prompts:** operator SYSTEM_PROMPT; customer chat linked/anonymous templates with the five
  escalation categories; cockpit prompt composed in bss-cockpit.

## 5. CLI (`cli/`, ≈ 7.2k non-test LOC)

Typer tree: 24 top-level groups, ~90 leaf commands (customer, case, ticket, order,
subscription, catalog, payment, promo, usage, prov, som, inventory, clock, trace, admin,
branding, scenario, onboard, external-calls, admin catalog/knowledge, `bss ask`). Bare `bss` →
REPL cockpit (repl.py 1.3k LOC: prompt-toolkit input/history, slash commands /sessions /new
/switch /reset /focus /ports /confirm /config /operator, Rich tables/panels, bss-cockpit ASCII
renderers, same propose-then-confirm + chrome guards as the browser cockpit). CLI links
orchestrator + clients **in-process** (no network hop). Scenario engine subpackage: YAML
runner with actions/assertions (jsonpath), freeze-clock setup, LLM `ask:` steps, reporting.
Note: no ASCII QR exists today (`qrcode` dep declared; only the self-serve portal renders PNG
QR) — do not invent one during the port.

## 6. Tooling, tests, infra

- **No CI config anywhere.** Validation = `make test` (~24 per-component pytest runs with
  PYTHONPATH isolation, `-m "not integration"`), `make lint` (ruff; mypy strict off the main
  gate), `make doctrine-check` (~21 grep/awk guards — enumerated in 02 §4), `make
  scenarios-hero` (19 hero YAML scenarios; sed-flips providers to mock, restores on trap),
  `make e2e` (compose overlay + Playwright + HTML/JUnit reports).
- **Tests hit real infra:** service conftests use httpx ASGITransport + real Postgres
  (`BSS_DB_URL` required, `make seed` assumed), no testcontainers, no DB fakes.
  ~1,746 test functions / 209 files. Top-level `tests/integration/phase_08` hits live
  localhost ports and auto-skips if closed.
- **Compose:** app stack (12 services, ports 8001–8010/9001/9002, single `bss` network,
  healthcheck-gated portal deps) + infra (pgvector/pg16, rabbitmq 3.13-mgmt, jaeger 1.65) +
  e2e overlay (env-only override pinning mock providers + LLM fixture).
- **Dockerfiles:** 2-stage python-slim (not distroless; curl for healthcheck; uid 1000).
- **Seed:** 1000 MSISDNs (90000000–90000999), 1000 eSIM profiles (ki_ref only, never Ki),
  PLAN_S/M/L, 4 VAS; idempotent ON CONFLICT DO NOTHING; demo seed with loyalty-cli pairing.
- **Env:** ~50 `BSS_*` vars in families (db/mq, clock, named tokens, LLM + autonomy, chat
  caps, OTel, portal auth ×14, email/KYC/payment/esim providers, branding, renewal, knowledge,
  loyalty).
- **External integration:** loyalty-cli (Bearer-auth HTTP, optional), OpenRouter, Stripe,
  Resend, Didit.

## 7. The six Python idioms that dominate porting risk

1. **ContextVar ambient context** — actor/channel/tenant/request-id/service-identity flow
   implicitly through async stacks (middleware sets; clients read; workers push/pop).
2. **Process-global mutable clock** — `bss_clock.now()` imported everywhere; freeze/advance
   admin endpoints make scenarios deterministic.
3. **Import-time side effects** — model registration for Alembic metadata; tool registration
   via decorator on module import; fail-fast env reads in constructors.
4. **SQLAlchemy identity-map semantics as load-bearing behavior** — `populate_existing=True`
   comments document real bugs; Rust's no-ORM-identity-map world deletes the hazard class but
   any logic relying on shared-session object identity must be found and rewritten.
5. **Dict-shaped payloads at seams** — inter-service responses and event payloads are
   `dict`s; Rust forces typed structs (good, but it's where silent behavior differences hide —
   especially the camelCase/snake_case dual families read via `bss_csr.views.field`).
6. **Pydantic v2 validators/aliasing** for TMF camelCase ↔ snake_case (29 files with
   validators) → serde + explicit validation functions; more verbose, must be systematic.
