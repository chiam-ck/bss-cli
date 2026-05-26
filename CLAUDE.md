# CLAUDE.md — BSS-CLI Project Doctrine (v3)

> **This file is your contract.** Read it at the start of every session. Do not deviate without an explicit Phase 0 amendment from the human.

## What this project is

**BSS-CLI** is a complete, lightweight, SID-aligned, TMF-compliant Business Support System designed to run entirely from a terminal. It is LLM-native: every operation is exposed as a tool the LLM can call, and the primary UI is the CLI plus ASCII-rendered visualizations. Analytics is out of scope in v0.x — `audit.domain_event` is the substrate; an external BI consumer plugs in BYOI against the shared Postgres if needed.

It is a **reference implementation** for engineers learning telco BSS/OSS, a **deployable MVP** for a small MVNO, and a **substrate** for agentic experiments against realistic telco operations. It covers CRM (with case/ticket management), Product Catalog, Commercial Order Management (COM), Service Order Management (SOM) with a provisioning simulator, eSIM profile management, Subscription & Bundle Balance, Mediation, Rating, and Payment.

## The seven motto principles (NEVER violate)

1. **Bundled-prepaid only.** No proration. No dunning. No collections. No credit risk modeling. The product is a bundle, the customer pays upfront via card-on-file, the bundle either has remaining quota or it doesn't.
2. **Card-on-file is mandatory.** Every customer has a payment method before activation. Failed charge equals no service. There is no grace period, no retry ladder, no manual collection workflow.
3. **Block-on-exhaust.** Service stops the instant a bundle hits zero. The only paths back to active are: bundle renewal (automatic, on period boundary, charged to COF) or VAS top-up (explicit customer action, charged to COF).
4. **CLI-first, LLM-native.** Every capability is a tool the LLM can call. The terminal is the primary interface. ASCII art is the visualization language. No graphical analytics dashboard ships in-tree — `audit.domain_event` is the substrate for anyone who wants to plug one in.
5. **TMF-compliant where it counts.** Real TMF Open API surfaces (TMF620, TMF621, TMF622, TMF629, TMF635, TMF638, TMF640, TMF641, TMF676, TMF678, TMF683). Not naming theater. Payloads match the spec.
6. **Lightweight is measurable.** The full stack runs in under 4GB RAM. Cold start under 30 seconds. p99 internal API latency under 50ms. If a change pushes us past these limits, it requires explicit justification.
7. **Write through policy, read freely.** Reads are free. Writes go through a validation layer that enforces domain invariants. There is no such thing as "raw CRUD" in BSS-CLI. The LLM cannot corrupt state even when asked to.

## Scope boundaries (what BSS-CLI is NOT)

These are things BSS-CLI deliberately does not do, and the channel layer or external systems are expected to handle:

- **eKYC.** Receive signed attestations; document capture / liveness / Myinfo / DigiLocker are channel-layer.
- **Customer-facing UI.** Mobile apps, web portals, retail POS, USSD — all channel layer.
- **Network elements.** HLR/HSS, PCRF, OCS, SM-DP+ — simulated in v0.1; real NE adapters are integration work.
- **eSIM redownload re-arm.** GSMA SGP.22 rearm ships post-v0.1 as a SOM task once the SM-DP+ adapter is real. v0.10's `/esim/<subscription_id>` is a read-only re-display, not a rearm. See DECISIONS 2026-04-27.
- **Physical SIM.** eSIM-only.
- **CDR collection from RAN.** Mediation accepts parsed CDRs via API; probe-side collection is out of scope.
- **Online Charging System (OCS).** Diameter Gy/Ro lives on the network side. Our Mediation is TMF635 *online* mediation: one event at a time, block-at-edge synchronously, balance decrement via events — not a batch pipeline, not an OCS.
- **Tax calculation.** v0.1 = SGD inclusive pricing. Vertex/Avalara is post-v0.1.
- **Regulatory reporting.** Extraction jobs against `audit.domain_event`, not built into services.

## Domain coverage

### CRM (lightweight but real)

- **Customer & Party.** TMF629. Customer, Individual, Contact Mediums, KYC attestation.
- **Interaction log.** TMF683. Every touchpoint logged automatically.
- **Case & Ticket management.** Custom Case aggregate + TMF621 Trouble Ticket. ServiceNow-shaped: Case parents 1..N Tickets.

### Order management (COM + SOM)

- **COM (TMF622).** Customer-facing. ProductOrder aggregate.
- **SOM (TMF641 + TMF638).** Technical. Decomposes COM orders into ServiceOrders, CFS, RFS, Resources.
- **Decomposition (v0.1):** PLAN_x → 1 CFS (MobileBroadband) → 2 RFS (Data, Voice) → {MSISDN, eSIM profile}.

### Provisioning simulator

Stands in for HLR/PCRF/OCS/SM-DP+. Configurable per-task-type fault injection. "Stuck" state for manual intervention.

### Inventory (eSIM + MSISDN)

Two pools, both managed by the Inventory domain:

- **MSISDN pool** — 1000 numbers seeded
- **eSIM profile pool** — 1000 profiles seeded, each with ICCID, IMSI, Ki-reference (NEVER raw Ki), SM-DP+ activation code

eSIM-only in v0.1. On order, SOM reserves both an MSISDN and an eSIM profile atomically. After activation, the customer receives an LPA activation code and ASCII-rendered QR code.

## The Write Policy doctrine

Every write in BSS-CLI flows through a policy layer that enforces domain invariants before the repository is touched. Router → Service → Policies → Repository → Event publisher. Always.

### Why

The LLM is powerful and will be asked to do things like "update Ck's plan" or "close all Ck's tickets". Without guardrails, an LLM can silently violate referential integrity, break state machines, orphan child records, or create impossible states. The policy layer is what lets us trust the LLM with write access.

### How

```
services/<svc>/app/
├── repositories/       # dumb CRUD over ORM
├── policies/           # invariant enforcement
├── services/           # orchestration — calls policies, not repositories
└── auth_context.py     # principal/tenant/roles (Phase 12-ready abstraction)
```

Policies validate BEFORE any write:
- **Referential invariants** — target exists, in right state, belongs to right parent
- **State machine invariants** — only legal transitions
- **Domain invariants** — e.g., "a Case cannot be closed with open Tickets"
- **Uniqueness invariants** — email, MSISDN, ICCID, external refs
- **Authorization invariants** — agent must be active, role must permit action (reads from auth_context)

On violation, a structured `PolicyViolation` is raised:

```json
{
  "code": "POLICY_VIOLATION",
  "rule": "case.close.requires_all_tickets_resolved",
  "message": "Case CASE-042 has 2 open tickets (TKT-101, TKT-103). Resolve them first.",
  "context": { "case_id": "CASE-042", "open_tickets": ["TKT-101", "TKT-103"] }
}
```

These errors flow back to the LLM as tool observations. The LLM reads the structured error and either retries with corrections or asks the user.

### No raw CRUD from CLI or LLM

Hard rule:
- CLI exposes verbs (`bss case open`), not entities (no `bss db update`)
- LLM has tools like `case.open`, not `case.update_raw`
- No escape hatch. Emergency data fixes happen via explicit admin tools which are themselves policy-gated

## Authentication & RBAC readiness

Three layers, each with its own contract:

**Perimeter token (v0.3 → v0.9 named tokens).** `BSSApiTokenMiddleware` (`packages/bss-middleware`) gates every BSS service's HTTP surface; missing/wrong `X-BSS-API-Token` → 401 before routing, timing-safe compare, fail-fast on `"changeme"`/<32-char at startup. Exempt paths are exactly `/health`, `/health/ready`, `/health/live`. v0.9 splits the single token into a `TokenMap` keyed by env-var name (`BSS_PORTAL_SELF_SERVE_API_TOKEN` → identity `"portal_self_serve"`; `BSS_OPERATOR_COCKPIT_API_TOKEN` → `"operator_cockpit"`; `BSS_API_TOKEN` → `"default"`). `service_identity` flows through `auth_context.AuthContext`, lands on `audit.domain_event.service_identity`, surfaces in structlog + OTel + `bss trace`. Outbound calls use `bss_clients.TokenAuthProvider` / `NamedTokenAuthProvider`. Rotation is restart-based (`docs/runbooks/api-token-rotation.md`).

**Customer-side auth (v0.8 portal session + v0.10 step-up).** `packages/bss-portal-auth` — email-based identity, server-side sessions, cookie carries session id only. `PortalSessionMiddleware` resolves the cookie, attaches `request.state.{session,identity,customer_id}`, rotates past TTL/2. Public-route allowlist in `bss_self_serve.security`; everything else gates on session. Login OTPs + magic links stored as HMAC-SHA-256 with `BSS_PORTAL_TOKEN_PEPPER`. Step-up auth required for sensitive writes (catalogue: `SENSITIVE_ACTION_LABELS`).

**Staff side — no login (v0.13).** Cockpit is single-operator-by-design behind a secure perimeter. `actor` comes from `.bss-cli/settings.toml` (descriptive, not verified). REPL (`bss`) is canonical; browser veneer at `localhost:9002/cockpit/<id>` mirrors the same `cockpit.session` / `cockpit.message` / `cockpit.pending_destructive` tables. `OPERATOR.md` prepended verbatim to every system prompt (hot-reloaded on mtime). `/confirm` flips `allow_destructive=True` for the next turn (destructive list = `bss_orchestrator.safety.DESTRUCTIVE_TOOLS`). `operator_cockpit` profile = full registry minus `*.mine` wrappers; `validate_profiles()` enforces at startup. **Phase-12 staff OAuth/RBAC is retired**, not deferred (DECISIONS 2026-05-01).

**Always-on (since v0.1):** `tenant_id` on every table, `X-BSS-Actor`/`X-BSS-Channel` headers plumbed everywhere, policy layer as the single write chokepoint, `bss-clients` as the single S2S chokepoint, `auth_context.current()` reads everywhere (never hardcoded).

## Design rules

- **No retry/dunning in payment.** Fail = block. Period.
- **No proration.** Bundles renew on clean period boundaries.
- **No partial payments.** Approved in full or declined in full.
- **No credit-checking.** COF presence is the only credit signal.
- **Time is explicit.** Never call `datetime.utcnow()` in business logic. Use the `clock` service. This makes scenarios deterministic.
- **State machines, not flags.** Subscription, Order, Service, Ticket, Case — all explicit FSMs with logged transitions.
- **Events are first-class.** Every meaningful state change emits a domain event AND persists to `audit.domain_event` in the same transaction.
- **No business logic in routers.** Routers → Services → Policies → Repositories. One-way.
- **Repositories never call services.** Lowest layer, no outward dependencies.
- **Inter-service calls via HTTP for synchronous needs, RabbitMQ for asynchronous reactions.** Never shared DB access across service boundaries.
- **Each service owns its schema** in a single Postgres instance (v0.1). The schema-per-service boundary enables a later split to one Postgres per service without touching service code.
- **(v0.7+) Subscription price is snapshotted at order time.** Renewal charges the snapshot, not the catalog. Catalog price changes affect new orders only; existing subscriptions migrate via an explicit operator-initiated flow (`subscription.migrate_to_new_price`) with regulatory notice.
- **(v0.12+) Chat is one modality of access, not a privileged path.** The same tool surface, under the same server-side policies, viewed through a tighter prompt-visible window. Customer chat sees the `customer_self_serve` profile (ownership-bound `*.mine` wrappers + public catalog reads); CSR/CLI/scenario callers keep full surface access. If the chat ever has a tool the customer's direct UI doesn't, that's a doctrine bug.
- **(v0.12+) The five escalation categories are not negotiable.** Fraud, billing dispute, regulator complaint, identity recovery, bereavement — all are `case.open_for_me` calls with no AI-side resolution attempt. The `EscalationCategory` Literal, the soak corpus, and the customer-chat system prompt encode the same list. Adding a sixth is a doctrine decision, not a scope decision.
- **(v0.13+) The REPL is the canonical operator cockpit.** The browser at `localhost:9002/cockpit/<id>` is a thin veneer over the same `Conversation` store. Slash-command parity is a doctrine target — if the REPL has `/focus` and the browser has no equivalent button, that's a doctrine bug to fix in the next sprint.
- **(v0.13+) Operator persona lives in `OPERATOR.md`.** Prepended verbatim to every cockpit system prompt. House rules are operator-editable; the cockpit's safety contract (propose-then-confirm, escalation list, ASCII rules) is code-defined in `bss_cockpit.prompts._COCKPIT_INVARIANTS`. An operator who wants to weaken the contract has to edit code, not a markdown file.

## Call patterns (HTTP vs events)

Two distinct planes:

**Synchronous HTTP (via bss-clients):** used when the caller needs an immediate answer.
- `payment.charge` (subscription needs approved/declined before activating)
- `customer.get` (CRM lookup during payment method creation)
- `catalog.get_offering` (order creation needs price)

**Asynchronous events (via RabbitMQ topic exchange `bss.events`):** used when the producer doesn't need an answer.
- `order.in_progress` (SOM reacts)
- `provisioning.task.completed` (SOM reacts)
- `service_order.completed` (COM reacts)
- `usage.rated` (Subscription decrements)

**Postgres is NOT a RabbitMQ consumer.** Each service writes directly to its own schema. The `audit.domain_event` row is written in the same DB transaction as the domain write; the RabbitMQ publish happens after commit (simplified outbox — best-effort delivery backed by the durable audit log).

## Tech stack (frozen for v0.x)

- **Language:** Python 3.12
- **Package manager:** `uv` with workspace layout
- **Web framework:** FastAPI (async everywhere)
- **ORM:** SQLAlchemy 2.0 async + asyncpg
- **Migrations:** Alembic
- **Validation:** Pydantic v2
- **State machines:** `transitions` library
- **Messaging:** RabbitMQ via `aio-pika`
- **CLI:** Typer + Rich
- **LLM orchestrator:** LangGraph
- **LLM gateway:** OpenRouter via the openai SDK (no LiteLLM hop) → `deepseek/deepseek-v4-pro` (v1.5.1+; previously `google/gemma-4-26b-a4b-it` v0.10.0–v1.5, swapped because Gemma needed too much defensive scaffolding to emit clean tool_calls — see DECISIONS 2026-05-26; before that, MiMo v2 Flash v0.10.0–, swapped for tool-call latency regression — DECISIONS 2026-04-27)
- **Database:** PostgreSQL 16, **single instance**, schema-per-domain (see ARCHITECTURE.md for future split path)
- **Vector DB (post-v0.1):** pgvector extension on the same Postgres instance (schema `knowledge`)
- **Reporting:** out of scope in-tree (BYOI against `audit.domain_event`)
- **Logging:** structlog (JSON)
- **Tracing (v0.2):** OpenTelemetry SDK + auto-instrumentors (FastAPI, HTTPX, AsyncPG, AioPika); OTLP/HTTP export to Jaeger
- **Auth (v0.3):** Shared `BSS_API_TOKEN` middleware (`packages/bss-middleware`) on every BSS service; `TokenAuthProvider` on every outbound client. Per-principal OAuth2 / JWT is Phase 12.
- **Portals (v0.4-v0.5):** FastAPI + Jinja + HTMX, server-rendered HTML, vendored `htmx.min.js` + `htmx-sse.js`. No React/Vue/Svelte, no bundler, no npm. Shared widgets in `packages/bss-portal-ui`.
- **Internal packages:** `bss-clients`, `bss-clock`, `bss-cockpit` (v0.13), `bss-events`, `bss-middleware`, `bss-telemetry`, `bss-portal-ui`, `bss-portal-auth`, `bss-admin`, `bss-models`, `bss-seed` — all under `packages/` as `uv` workspace members.
- **Testing:** pytest + pytest-asyncio + httpx AsyncClient
- **Linting:** ruff + black + mypy
- **Container:** multi-stage Dockerfiles, non-root users, distroless final stage where practical

## Deployment model

9 service containers + 2 portal containers (self-serve 9001, cockpit veneer 9002) + three optional infra containers (Postgres, RabbitMQ, Jaeger). Billing deferred to v0.2 — port 8009 reserved (DECISIONS 2026-04-13). REPL (`bss`) is canonical cockpit; browser is the same Conversation viewed in HTML. See `ARCHITECTURE.md` for topology, compose profiles, and the AWS path.

## Naming conventions

- **Python modules:** snake_case
- **Python classes:** PascalCase
- **DB tables:** snake_case, singular (`customer`, not `customers`)
- **DB schemas:** snake_case domain name (`crm`, `catalog`, `order_mgmt`, `service_inventory`, `provisioning`, `inventory`, `knowledge`)
- **TMF payloads:** camelCase (match the spec exactly)
- **Internal DTOs:** snake_case
- **Event routing keys:** dot-separated lowercase (`subscription.exhausted`, `ticket.assigned`)
- **IDs:** prefixed strings (`CUST-001`, `ORD-014`, `SUB-007`, `CASE-042`, `TKT-101`, `SO-222`, `SVC-333`, `PTK-444`). UUIDs internally in DB are fine; the surface is always prefixed.

## Anti-patterns (never do these)

> Many of the per-version rules below are enforced by `make doctrine-check` greppables — those are abbreviated here. The full grep patterns live alongside the guard sources. Older rationales (v0.4–v0.16 commit history, retired paths, deferred features) live in `DECISIONS.md` and the git tags.

**Always:**

- Don't put business logic in Typer command handlers. CLI calls orchestrator or bss-clients, nothing more.
- Don't mix sync and async code paths.
- Don't catch exceptions in routers. Let middleware handle them.
- Don't log card numbers, tokens, full NRIC, full Ki values, or full ICCIDs beyond last-4. Use the structlog redaction filter.
- Don't add retries inside tool functions. LangGraph supervisor handles retries.
- Don't reach for a rules engine for rating. Tariff is JSON; rating is a pure function.
- Don't bake in channel-specific logic. BSS-CLI is channel-agnostic.
- Don't implement eKYC flows. Receive attestations, record them, enforce policies.
- Don't hardcode ports, URLs, or model names. Everything via env.
- Don't commit mid-phase. Verify first, commit after.
- Don't let Claude Code modify `CLAUDE.md`, `DATA_MODEL.md`, `ARCHITECTURE.md`, or `TOOL_SURFACE.md` without an explicit Phase 0 amendment.
- Don't bypass the policy layer. If you need to, the policy is wrong and needs amending.

**Portals & orchestrator boundary (v0.4–v0.12):**

- HTMX + SSE, no React/Vue/Svelte. (v0.4+)
- Pass `actor=<operator_id>` to `astream_once`; don't attribute agent actions to `channel=llm` when the operator is human. (v0.5+)
- Use `bss_clock.now()` — never `datetime.now()`/`utcnow()` in business logic. Grep guard enforces. (v0.5+)
- Read snapshot off the subscription at renewal — don't query catalog active prices. (v0.7+)
- Plan changes pivot via pending fields at renewal — never terminate-and-recreate. (v0.7+)
- Only the chat surface goes through the orchestrator (`rg 'astream_once' portals/self-serve/bss_self_serve/routes/` matches `chat.py` only). Signup writes directly via `bss-clients`. Cap-trip → templated SSE; `AgentOwnershipViolation` → generic safety reply. The OpenRouter API key never leaves the orchestrator process. (v0.11–v0.12)

**Customer-side perimeter (v0.8–v0.10):**

- Read `customer_id` from `request.state.customer_id`; never accept it (or `subscription_id`/`service_id`/`payment_method_id`) from form/body/query/path. Cross-customer attempts → 403 via `check_*_owned_by` policies. Grep guard.
- Login OTPs + magic-link tokens stored as HMAC-SHA-256 with the server pepper, timing-safe compare. `bss_portal_auth` helpers are the only path. Grep guard against logging.
- Public marketing pages stay on the allowlist (`/welcome`, `/plans`, `/auth/*`, `/static/*`, `/portal-ui/static/*`). New public route = allowlist entry + test.
- Route handlers don't touch the cookie header — `PortalSessionMiddleware` is the only path. Grep guard.
- One route → one `bss-clients` write (or zero). Compositions belong in the orchestrator or in a service-side composite. Single exception: cross-schema email-change (explicit `crm` + `portal_auth` transaction).
- Step-up auth gates every label in `SENSITIVE_ACTION_LABELS` (`portals/self-serve/bss_self_serve/security.py`). New sensitive route = label entry + `requires_step_up(label)` dep. Test enforces both directions.

**Named tokens (v0.9):**

- Don't trust `X-BSS-Service-Identity` (or any sibling) header — `service_identity` comes from `TokenMap` validation only. Grep guard.
- One named token per surface; never share. Rotation per surface, not per leak.
- Tokens load once at startup; never `os.environ` reads at request time. Grep guard.

**Customer-self-serve tool profile (v0.12):**

- `*.mine` / `*_for_me` tools bind from `auth_context.current().actor` — never accept `customer_id`/`customer_email`/`msisdn` as a parameter. `validate_profiles()` enforces.
- Extending the `customer_self_serve` profile requires a security review and a matching `OWNERSHIP_PATHS` entry. Checklist: runbook "Adding a tool to the customer_self_serve profile".

**Cockpit (v0.13):**

- Don't reach for OAuth/RBAC for staff. Single-operator-by-design behind a secure perimeter; multi-operator separation = multi-tenant carve-out, not a login wall. (DECISIONS 2026-05-01)
- Don't read `OPERATOR.md` or `settings.toml` outside `bss_cockpit.config` — hot-reload (mtime cache) is the contract.
- Don't bypass `/confirm` for destructive actions. The `cockpit.pending_destructive` row is the contract; only consuming it flips `allow_destructive=True`.
- Don't add a Conversation store to a service. Cockpit conversations live in the `cockpit` schema owned by `bss-cockpit`; portals + REPL consume.
- Don't accept user-controllable `session_id` outside cockpit routes. Both REPL `--session` and browser path are operator-typed inputs only.
- Don't store secrets in `settings.toml`. Anything that would burn the perimeter if leaked stays in `.env`.

**Cockpit (v1.5 — multi-step autonomy):**

- Don't read `BSS_REPL_LLM_AUTONOMY` outside `bss_orchestrator.autonomy`. `read_autonomy_mode()` is the single seam; fail-closed boot validation (`AutonomyMisconfigured`) is the contract — same shape as the v0.9 named-token sentinel rejection. The cached mode flows via `app.state.autonomy_mode` (cockpit portal) and the REPL's module-level `_AUTONOMY_MODE`.
- Don't add the `ITERATIVE FLOW` prompt block to the `customer_self_serve` chat surface (`orchestrator/bss_orchestrator/customer_chat_prompt.py`). Compound actions are an operator capability — the v0.12 chat caps + ownership trip-wire were never stress-tested against agent-driven write chains. Doctrine guard: `orchestrator/tests/test_iterative_flow_scope.py` asserts the absence in customer chat AND the presence in the operator prompt.
- Don't change `MAX_CONSECUTIVE_TOOL_FAILURES` from 3 without updating the test guard, the soak corpus expectations, and the cockpit's "couldn't recover" panel copy. Three is the lift from loyalty-cli; raising it invites longer thrash, lowering it cuts off healthy investigation loops.
- Don't extend the cockpit's emit-chrome set without adding the new prefix to `bss_cockpit.chrome_filter._ASSISTANT_CHROME_PREFIXES`. The inventory-lock test pins the set; an omission lets chrome back into LLM history and re-opens the mimicry/state-confusion/citation-thrash failure modes from v1.5's design notes.

**Provider adapters & webhooks (v0.14):**

- Per-domain adapter Protocols only — no unified `Provider.execute()` API.
- Webhook receivers authenticate via provider signature inside the handler (svix/stripe/didit_hmac); don't put them behind `BSSApiTokenMiddleware`. `WEBHOOK_EXEMPT_PATHS` lists `/webhooks/`.
- Use `bss_webhooks.redaction.redact_provider_payload()` before logging or persisting raw provider bodies. Grep guard.
- Provider config = `.env` only through v0.16; tenant-scoped DB config is post-v0.16.
- Never silently fall back to `MockAdapter` when prod creds are missing — `select_*` raises `RuntimeError` with the missing-var name.
- Provider name is resolved at startup onto `app.state` — never accepted from a request body. Same logic as the v0.9 service-identity rule.
- Provider env vars load once at startup; never per-request. Grep guard.

**KYC (v0.15):**

- No `services/kyc/` container. KYC is channel-layer; BSS verifies signed attestations only.
- Reduce KYC PII at the boundary: `document_number_last4` + `document_number_hash` (SHA-256, domain-separated) inside `DiditKycAdapter.fetch_attestation`. Names, addresses, biometric URLs, MRZ, parsed-address, liveness scores never cross. Grep guard.
- Trust anchor for Didit attestations is the corroborating verified-webhook row in `integrations.kyc_webhook_corroboration`; policy `check_attestation_signature` rejects uncorroborated.
- No silent fallback when Didit caps out — raise `KycCapExhausted`, emit `kyc.cap_exhausted`, show retry page.
- `provider="prebaked"` (or legacy `myinfo`) requires `BSS_KYC_ALLOW_PREBAKED=true` in production. Default reject when `BSS_ENV=production`.
- `BSS_KYC_ALLOW_DOC_REUSE=true` is sandbox-only — production must default false.
- No JWKS validator for Didit — the decision API has no JWS today (probed 2026-05-02). Grep guard against `BSS_PORTAL_KYC_DIDIT_JWKS_URL`.
- `BSS_ESIM_PROVIDER=onbglobal`/`=esim_access` fail on first use, not at startup.

**Payment (v0.16):**

- No server-side card-number inputs when `BSS_PAYMENT_PROVIDER=stripe` and `BSS_ENV=production` (PCI SAQ A). Server-side `tokenize` raises `NotImplementedError`. Startup template scan refuses to boot.
- `charge.dispute.created` is record-only via `payment.dispute_opened` event — don't auto-open a case. (Motto #1)
- Out-of-band refunds are recorded; balance reversal is operator-initiated.
- Idempotency keys: same key only on BSS-crash-restart retries (v1.0); user-initiated retries always get a fresh key (`r0`).
- All charges go through `self._tokenizer.charge(...)` (constructor-injected `TokenizerAdapter`); no direct `mock_tokenizer.charge` from `payment_service.py`. Grep guard.
- Switching payment providers requires a documented cutover (`bss payment cutover --invalidate-mock-tokens` or accept lazy-fail). Lazy-fail policy: `payment.charge.token_provider_matches_active`. Runbook: `docs/runbooks/stripe-cutover.md`.
- Stripe webhook on `signature_invalid` always logs `candidate_headers` (truncated, redacted) + `body_preview` — diagnostic from day one (v0.15 lesson, commit `a17a5c9`).
- Webhook reconciliation dedupes on `(provider, event_id)` for retries but updates the `payment_attempt` row last-write-wins per `payment_intent_id` — don't gate on "first time we've seen this session".
- Synchronous charge response is primary truth for `payment_attempt.status`; webhooks reconcile and emit `payment.attempt_state_drift` on mismatch — don't overwrite.
- `BSS_PAYMENT_ALLOW_TEST_CARD_REUSE=true` is sandbox-only. `select_tokenizer` refuses it paired with `sk_live_*` at startup.

**MNP & roaming (v0.17):**

- Port requests are their own aggregate (`crm.port_request`) with their own FSM and audit-event family — don't overload `Case`.
- Ported-out MSISDN status `ported_out` is *terminal* with `quarantine_until='9999-12-31'`; never release back to `available`. Grep guard.
- Roaming is a per-event attribute (`roaming_indicator: bool`), not a new `event_type`. `VALID_EVENT_TYPES` stays at `{data, voice, voice_minutes, sms}`.
- Roaming-balance exhaustion does not cascade into home-data exhaustion. `data_roaming` is additive; `is_exhausted` considers primary allowances only.
- Port-request writes are `operator_cockpit`-only — no `customer_self_serve` exposure.

**Renewal (v0.18):**

- Renewal trigger lives only in the subscription lifespan tick loop (`services/subscription/app/workers/renewal.py`). No sibling scheduler, no cron, no Celery beat. `FOR UPDATE SKIP LOCKED` is the multi-replica safety. Manual `subscription.renew_now` is the operator escape hatch. Grep guard.
- Worker calls `service.renew(sub_id)` and nothing else. No parallel state machine, no inline charge, no transition calls. Grep guard.

**Knowledge & doctrine (v0.20):**

- Don't index `phases/V0_*.md`. Allowlist in `bss_knowledge.paths.INDEXED_PATHS` is source of truth; doctrine guard 16 enforces.
- `knowledge.search`/`knowledge.get` are `operator_cockpit`-only — never `customer_self_serve`. Doctrine guard 15.
- Cockpit cites or says "I don't have a citation for that". `_RE_KNOWLEDGE_CLAIM` guard replaces un-cited handbook claims with a fallback pointing at `bss admin knowledge search`. A reliably-tripped model = search-index bug or prompt regression; fix the index/prompt, not the regex.
- Roaming offerings use `--data-roaming-mb` on `add-offering` (v0.17–v0.19 SQL workaround retired).

## Project meta

- **License:** Apache-2.0
- **Repo:** monorepo, `uv` workspace
- **Branching:** `main` is shippable; feature branches per phase
- **Commits:** Conventional Commits (`feat(crm): add case lifecycle`), one commit per phase minimum
- **CI:** GitHub Actions, runs linters + tests + hero scenarios on every PR (added post-Phase 10)
- **Versioning:** SemVer. v0.1.0 = first shippable demo.
