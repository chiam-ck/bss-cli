# ARCHITECTURE.md — BSS-CLI (v3)

## Topology

Three callers — **CLI** (terminal-native), **self-serve portal** (public signup, port 9001), and **CSR console** (operator workbench, port 9002) — reach the 9 services through one of two paths: **direct via `bss-clients`** for deterministic routine flows (every CLI/REPL call, every read, every post-login self-serve write, every signup step from v0.11), or **orchestrator-mediated via `astream_once`** for flows that need LLM judgment (the CSR `ask` agent surface, the chat route on the self-serve portal). Inside, two planes connect the 9 services: **synchronous HTTP (TMF APIs)** for calls that need an immediate answer, and **asynchronous events (RabbitMQ topic exchange)** for reactions. Postgres is accessed directly by each service's own writes — the message broker is not a database pipe.

```
   ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────────────┐
   │  Self-serve UI   │  │  CSR console UI  │  │  bss (CLI + REPL)        │
   │  port 9001 (v0.4)│  │  port 9002 (v0.5)│  │  + LangGraph Orchestrator│
   └────────┬─────────┘  └─────────┬────────┘  └────────────┬────────────┘
            │                      │                        │
   ┌────────┴───────┐    ┌─────────┴────────┐               │
   │ direct         │    │ ask agent surface│               │
   │  bss-clients   │    │  agent_bridge.*  │               │
   │ (signup, post- │    │  → astream_once  │               │
   │  login, reads) │    └─────────┬────────┘               │
   │ chat → astream │              │                        │
   │ (customer_     │              │                        │
   │  self_serve    │              │                        │
   │  profile,      │              │                        │
   │  v0.12)        │              │                        │
   └────────┬───────┘              │                        │
            │                      ▼                        ▼
            │      ┌──────────────────────────────────────────────────┐
            │      │  bss_orchestrator.session.astream_once(channel,  │
            │      │    actor=…) · ReAct over tool registry · pin     │
            │      │    allow_destructive=False                       │
            │      └──────────────────────────┬───────────────────────┘
            │                                 │
            └────────────────────►────────────┴───────────────────────►
                                      │ HTTP (TMF APIs) + bss-clients
        ┌──────┬────────┬─────────────┼────────┬────────┐
        ▼      ▼        ▼             ▼        ▼
     ┌─────┐┌─────┐ ┌─────┐       ┌─────┐┌─────┐
     │CRM* ││Pay  │ │Cat  │       │COM  ││Subs │
     │8002 ││8003 │ │8001 │       │8004 ││8006 │
     └──┬──┘└──┬──┘ └──┬──┘       └──┬──┘└──┬──┘
        │      │       │             │      │
        │      └───HTTP (e.g. Pay→CRM "customer exists?")
        │                            │
        │      ┌─────────────────────┼──────────────────────┐
        │      │         ┌─────┐┌─────┐ ┌─────┐ ┌─────┐
        │      │         │SOM  ││Med  │ │Rate ││Prov │
        │      │         │8005 ││8007 │ │8008 ││Sim  │
        │      │         └──┬──┘└──┬──┘ └──┬──┘ │8010 │
        │      │            │      │       │    └──┬──┘
        │      │            │      │       │       │
        ▼      ▼            ▼      ▼       ▼       ▼
     ═══════════════════════════════════════════════════════════
     ║         RabbitMQ — topic exchange: bss.events            ║
     ║  order.* · service_order.* · service.* · provisioning.*  ║
     ║  subscription.* · usage.* · crm.* · payment.*            ║
     ═══════════════════════════════════════════════════════════

     Each service writes directly to its own schema in ONE shared
     Postgres instance. audit.domain_event is written in the same
     transaction as the domain write; RabbitMQ publish happens
     after commit (simplified outbox). Every service exports OTel
     spans to Jaeger (v0.2+).

     ┌──────────────────────────────────────────────────┐  ┌──────────────┐
     │             PostgreSQL 16 (single instance)       │  │   Jaeger     │
     │                                                    │  │  (v0.2+)     │
     │  crm · catalog · inventory · payment · order_mgmt │  │  OTLP/HTTP   │
     │  service_inventory · provisioning · subscription  │  │  → traces UI │
     │  mediation · billing · audit · knowledge          │  └──────────────┘
     └──────────────────────────────────────────────────┘

     External sibling adapter (v1.1, OPTIONAL):
       loyalty-cli  ─ promo entitlements (Bearer-auth HTTP at
                      loyalty-http:8080; catalog + com + crm hold the
                      client). Unset BSS_LOYALTY_API_TOKEN → promo
                      subsystem off, every core flow runs unchanged
                      (graceful degradation).
```

**\* CRM hosts the Inventory sub-domain** (MSISDN pool + eSIM profile pool) on port 8002 under `/inventory-api/v1/...`. Not a separate container in v0.1. See "Services" table below.

## Call patterns

### Synchronous HTTP (via bss-clients)

Used when the caller needs an immediate answer.

| Caller → Callee | Purpose |
|---|---|
| CLI/orchestrator → any service | User-facing request |
| Payment → CRM (customer_exists) | Pre-write validation |
| COM → Catalog (get_offering) | Pre-write validation |
| COM → Subscription (create on order complete) | COM waits for subscription ID |
| Subscription → Payment (charge) | Need approved/declined before activate |
| SOM → CRM Inventory (reserve_msisdn + reserve_esim) | Atomic reservation on shared CRM instance |
| CRM (close policy) → Subscription (list_for_customer) | Policy needs live answer |
| Mediation → Subscription (get_by_msisdn) | Enrichment |
| Catalog + COM + CRM → loyalty-cli (v1.1, OPTIONAL) | Promo entitlements via Bearer-auth `LoyaltyClient`. Catalog registers offer definitions in the create saga; CRM pairs customer↔offer at `promo assign` (v1.3); COM claims/validates the discount at order completion. Unset `BSS_LOYALTY_API_TOKEN` → promo subsystem off, every core flow runs unchanged. |

### Asynchronous events (RabbitMQ)

Used when the producer doesn't need an answer and N consumers may care.

| Publisher → Routing Key | Consumers |
|---|---|
| COM → `order.in_progress` | SOM |
| SOM → `provisioning.task.created` | Provisioning-sim |
| Provisioning-sim → `provisioning.task.completed` | SOM |
| SOM → `service_order.completed` / `service_order.failed` | COM |
| COM → `order.completed` | Subscription (activation trigger) |
| Subscription → `subscription.activated` / `exhausted` / `blocked` | (future: notification, analytics) |
| Mediation → `usage.recorded` | Rating |
| Rating → `usage.rated` | Subscription |

**Event exchange:** single topic exchange `bss.events`. Consumers bind queues with routing patterns (e.g., SOM binds `order.in_progress`, `provisioning.task.completed`).

**Outbox pattern (simplified):** every service writes to `audit.domain_event` inside the same DB transaction as the domain write. After the transaction commits, a post-commit hook publishes to RabbitMQ best-effort. If RabbitMQ is down, the audit row is still present and a replay job (post-v0.1) can republish.

### Event ordering guarantees

RabbitMQ topic exchange preserves **order within a single routing key for a single consumer**. It does NOT preserve order between different routing keys, and parallel consumers on the same queue can observe events in different orders than published.

Consequences for BSS-CLI:

- **SOM receives `provisioning.task.completed` in arrival order**, not necessarily publish order. When 5 tasks complete roughly simultaneously, SOM sees them in some unspecified order. The `service.activate.requires_all_rfs_activated_and_esim_prepared` policy is the thing that enforces causality — activation only proceeds when all prerequisites are met, regardless of which arrived first.

- **Scenario `expect_event_sequence` assertions must describe causal order, not strict publish order.** When two events are concurrent (e.g., two parallel `task.started` events for different RFS), accept either ordering. The test framework polls for the sequence relation, not the exact interleaving.

- **`audit.domain_event` is ordered by `occurred_at` (post-commit timestamp).** For events published within seconds of each other from different services, the timestamps may reflect commit order, not causal order. Close reads usually agree with the mental model; races may not. When debugging a chain, trust the causal relationships (parent→child, request→response) over the absolute timestamps.

If strict ordering becomes essential for a future use case, the path forward is RabbitMQ routing by `subscription_id` (or another partition key), so all events for one subscription land on the same consumer queue. That's Phase 11+ territory; v0.1 does not need it.

## Services (9 total)

| # | Service | Port | TMF | State | Notes |
|---|---|---|---|---|---|
| 1 | catalog | 8001 | TMF620 | stateless | Read-only in v0.1 |
| 2 | crm | 8002 | TMF629 + TMF621 + TMF683 | stateful | Customer + Case + Ticket + Interaction + KYC + **Inventory sub-domain** |
| 3 | payment | 8003 | TMF676 | stateful | Mock gateway |
| 4 | com | 8004 | TMF622 | stateful FSM | Commercial Order Management |
| 5 | som | 8005 | TMF641 + TMF638 + TMF640 | stateful FSM | Service Order + decomposition |
| 6 | subscription | 8006 | custom | stateful FSM | Bundle balance, VAS, renewal |
| 7 | mediation | 8007 | TMF635 | stateful | **TMF635 online mediation.** Single-event ingest, block-at-edge, not batch. OCS is abstracted outside BSS-CLI — see "What's NOT in the architecture". |
| 8 | rating | 8008 | — | stateless | Pure rating function + consumer. Bundled-prepaid quota decrement, not per-unit billing-rate CDR rating. |
| 9 | provisioning-sim | 8010 | custom | stateful | Fake HLR/PCRF/OCS/SM-DP+, configurable failures |

Port 8009 is reserved for the v0.2 billing service — see the "Note on billing in v0.1" subsection below and `DECISIONS.md` 2026-04-13.

## Portals (channel layer, v0.4+)

A portal is a **channel** onto the BSS — a thin HTTP surface that translates a specific audience's actions (self-serve customers, CSRs, retail partners) into tool calls the LLM orchestrator runs against the 9 core services. Portals live under `portals/` in the repo, not under `services/`; each one ships as its own container on the **9xxx port range**. v0.4 shipped the self-serve portal; v0.5 adds the CSR console.

| # | Portal | Port | Audience | Writes go through… | Inbound auth |
|---|---|---|---|---|---|
| 1 | self-serve | 9001 | Prospect browsing / signing up + post-login customer self-serve + chat | **(v0.11+)** All routine flows — signup funnel + post-login self-serve — write *directly* via `bss-clients`. The chat surface (v0.12) is the *only* orchestrator-mediated route, invoked via `astream_once(channel="portal-chat", actor=customer_id, tool_filter="customer_self_serve", service_identity="portal_self_serve")`. | **email + magic link / OTP (v0.8)**; chat additionally caps + scopes per customer (v0.12). See "Portal authentication" + "Chat scoping" below. |
| 2 | csr | 9002 | Operator cockpit (browser veneer over the v0.13 cockpit Conversation store; CLI REPL is canonical) | The cockpit chat route — `routes/cockpit.py /cockpit/{id}/events` — drives `astream_once(channel="portal-csr", actor=settings.actor, service_identity="operator_cockpit", tool_filter="operator_cockpit")`. All other cockpit routes (sessions index, thread page, focus / reset / confirm POSTs) use the shared `Conversation` store directly. | **None.** v0.13 retired the v0.5 stub-login pattern (DECISIONS 2026-05-01); the cockpit runs single-operator-by-design behind a secure perimeter. `actor` from `.bss-cli/settings.toml` (descriptive, not verified). |

The portal-write story split (consolidated through v0.12):

* **(v0.4–v0.10)** *Historical:* signup + chat routed through the LLM orchestrator; v0.4 shipped the agent-log SSE widget as the demo artifact for "the agent pattern works on a customer-facing flow."
* **(v0.10+)** Post-login customer self-serve routes write *directly* via `bss-clients` from the route handler. The customer principal is bound from `request.state.customer_id` (verified session); per-resource ownership policies and step-up auth gate sensitive writes; one route = one BSS write.
* **(v0.11+)** The signup funnel joins the direct-write side. Signup is a deterministic routine flow (pick plan → MSISDN → KYC attest → COF → place order → poll for activation); each step has one correct next step and benefits nothing from LLM reasoning. Wall time per signup drops from ~85s (orchestrator-mediated) to under 10s (direct). The chat surface is now the *only* orchestrator-mediated route in the self-serve portal.
* **(v0.12+)** Chat ships, scoped to the logged-in customer via the `customer_self_serve` tool profile (16 curated tools: 3 public catalog reads + 8 read `*.mine` wrappers + 4 write `*.mine` wrappers + `case.open_for_me`). No `*.mine` tool accepts `customer_id` — the binding comes from `auth_context.current().actor`, set per-stream from `request.state.customer_id`. Output ownership trip-wire (`assert_owned_output` + `OWNERSHIP_PATHS`) catches the day a server-side policy misses a case. Per-customer rate + monthly cost caps (`audit.chat_usage`, fail-closed). Five non-negotiable escalation categories — fraud, billing_dispute, regulator_complaint, identity_recovery, bereavement — via `case.open_for_me` with SHA-256-hashed transcript stored in `audit.chat_transcript` + linked from `crm.case.chat_transcript_hash`. See "Chat scoping" below.

```
┌─ portal-self-serve (9001) ──────────────────────────────────┐
│                                                              │
│  chat (/chat, /chat/widget, /chat/events/{sid}) ────────────►│ ← orchestrator (v0.12 only)
│      → astream_once(tool_filter="customer_self_serve",       │   16-tool profile,
│                     actor=customer_id, ...)                  │   trip-wire, caps,
│                                                              │   5 escalation cats
│                                                              │
│  DIRECT (v0.10+ post-login, v0.11+ signup)                   │
│    /signup/{plan}/msisdn   ─────► inventory.list_msisdns    │
│    POST /signup            ─────► customer.create +         │
│                                    customer.attest_kyc +    │
│                                    payment.add_card +       │
│                                    com.create_order         │
│                                    (chained step routes —   │
│                                    one BSS write per route) │
│    /activation/{order_id}  ─────► com.get_order (poll)      │
│    /confirmation/{sub_id}  ─────► subscription.get +        │
│                                    inventory.get_activation │
│    /                       ─────► subscription.list_for_   │ ─► direct via bss-clients
│    /top-up                 ─────► subscription.purchase_   │   (NamedTokenAuthProvider —
│    /payment-methods/*      ─────► payment.{create,remove,  │    "portal_self_serve")
│                                    set_default}_method     │
│    /esim/<id>              ─────► subscription.get +       │
│                                    inventory.get_activ.    │
│    /subscription/<id>/cancel ───► subscription.terminate   │
│    /profile/contact/*      ─────► customer.update_contact_ │
│                                    medium + cross-schema   │
│                                    email-change            │
│    /billing/history        ─────► payment.list_payments +  │
│                                    count_payments          │
│    /plan/change*           ─────► subscription.schedule_   │
│                                    plan_change + cancel    │
└──────────────────────────────────────────────────────────────┘
```

Reads have always gone direct (the doctrine never required mediating a pass-through GET). The v0.10 / v0.11 carve-outs extend that posture to *deterministic* writes — routine flows where an LLM round-trip is latency tax with no judgment-quality benefit. Routes that genuinely need LLM judgment (the chat surface) remain orchestrator-mediated; the V0_11 doctrine commitment is that the LLM is in the path **only** where it adds value.

- **Reads go direct.** Listing offerings, fetching a customer 360, polling order state — all direct `bss-clients` calls. LLM-mediating a pass-through read is pointless latency.
- **`X-BSS-Channel` attribution.** Every outbound call carries the portal's channel name (`portal-self-serve` or `portal-csr`) so CRM's interaction auto-log attributes the write to the right surface. The hero scenarios assert this.
- **`X-BSS-Actor` carries the human (v0.5+).** The CSR portal sets `actor=<operator_id>` on every outbound call so the interaction log shows *who* asked, not which model executed. Per-model attribution still lives in `audit.domain_event.actor` (`llm-<model-slug>`).
- **`BSS_API_TOKEN` on outbound only.** Portals' inbound HTTP surfaces are not gated by `BSSApiTokenMiddleware` (different auth stories per portal — see the table). Their outbound calls through `TokenAuthProvider` are authenticated like any other v0.3+ caller.
- **Pure server-rendered HTML + HTMX.** No React/Vue/Svelte, no bundler, no npm.

### Shared package: `packages/bss-portal-ui` (v0.5+)

The agent-log widget primitives, SSE plumbing helpers (`format_frame`, `status_html`), event-projection logic (`project`, `render_html`), base CSS (palette + layout primitives + agent-log + chat-bubble styling), and vendored HTMX (`htmx.min.js` + `htmx-sse.js`) live in a single shared package. Both portals consume it via:
- a Jinja `ChoiceLoader` that resolves portal-local templates first then falls back to the package's shared partials, and
- a `StaticFiles` mount at `/portal-ui/static/` that serves the package's CSS + JS.

Extracted in v0.5 (before the second portal was written) to prevent the agent-log widget from drifting between portals — a fix landing only in self-serve when both need it would surface as a demo bug a month later. Documented in `DECISIONS.md` 2026-04-23.

**Where the streaming-tool-call surface lives now:** the v0.4 self-serve signup widget is retired (signup went direct in v0.11). The v0.5 CSR `ask`-on-customer-360 flow is retired (PR7 in v0.13 collapsed the entire portal). The chat-bubble HTML renderers (`render_assistant_bubble` / `render_tool_pill` / `render_chat_markdown`) were extracted from `routes/chat.py` to `bss_portal_ui.chat_html` in v0.13 PR5 so the operator cockpit thread renders identically to the customer chat surface. Both surfaces share `format_frame` + `status_html` for SSE wire format. Details in `phases/V0_4_0.md`, `phases/V0_5_0.md` (retired routes), `phases/V0_12_0.md` (chat surface), and `phases/V0_13_0.md` (cockpit + helper extraction).

### Named-token perimeter (v0.9)

v0.9 splits the v0.3 single-token model so each external-facing surface carries its own identity at the BSS perimeter. v0.13 added a third named token, `BSS_OPERATOR_COCKPIT_API_TOKEN`, for the cockpit. The orchestrator keeps using `BSS_API_TOKEN` (default identity); the self-serve portal carries `BSS_PORTAL_SELF_SERVE_API_TOKEN` (`portal_self_serve`); the cockpit carries `BSS_OPERATOR_COCKPIT_API_TOKEN` (`operator_cockpit`). The diagram below shows the post-v0.9 token flow.

```
                ┌─────────────┐
                │   browser   │
                └──────┬──────┘
                       │ session cookie (no BSS token here — server-side only)
                       ▼
   ┌──────────────────────────┐         ┌──────────────────────────┐
   │ portal-self-serve (9001) │         │ csr-console (9002)       │
   │  outbound: NamedToken    │         │  outbound: TokenAuth     │
   │  → BSS_PORTAL_SELF_SERVE_API_TOKEN  │         │  → BSS_API_TOKEN         │
   │  identity: portal_self_  │         │  identity: default       │
   │            serve         │         │                          │
   └──────────────┬───────────┘         └──────────────┬───────────┘
                  │                                    │
                  ▼                                    ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │              orchestrator (cli + scenario runner)               │
   │              outbound: TokenAuth → BSS_API_TOKEN                │
   └─────────────────────────────┬───────────────────────────────────┘
                                 │ X-BSS-API-Token: <one of N>
                                 ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │   BSSApiTokenMiddleware (every BSS service)                     │
   │   • TokenMap loaded once at startup from BSS_*_API_TOKEN envs   │
   │   • hashed (HMAC-SHA-256, fixed salt) for safe debug logs       │
   │   • on hit: scope["service_identity"] = <derived from token>    │
   │   • on miss: 401 (rate-limit-aware log policy)                  │
   └─────────────────────────────┬───────────────────────────────────┘
                                 ▼
                   ┌──────────────────────────────┐
                   │ RequestIdMiddleware          │
                   │ • reads scope.service_identity
                   │ • → auth_context             │
                   │ • → structlog ctxvars        │
                   │ • → OTel server span attr    │
                   └──────────────┬───────────────┘
                                  ▼
                          route → policy → repo
                                  │
                                  ▼
                       audit.domain_event
                       (service_identity column,
                        backfilled to 'default'
                        for pre-v0.9 rows)
```

**Key invariants:**
- Identity is *resolved* (validated token → identity), never *asserted* (no `X-BSS-Service-Identity` header is trusted).
- Each surface carries a distinct token. Sharing a named token across surfaces collapses the blast-radius reduction.
- Rotation is restart-based and per-token. A leaked portal token rotates without disturbing orchestrator/CSR.

Phase 12 swap: replace `BSSApiTokenMiddleware` with a JWT validator. `auth_context.py` reads claims instead of headers. The named-token model is the bridge — it leaves the principal/role layer untouched while distinguishing surfaces, so the Phase 12 step is mechanical.

### Portal authentication (self-serve, v0.8)

v0.8 puts a login wall in front of the self-serve portal. The CSR (operator) surface is **not** auth-gated — v0.13 retired the v0.5 stub-login pattern entirely; the cockpit runs single-operator-by-design behind a secure perimeter (see "v0.13 operator cockpit" below). Schema, library, and middleware on the customer-side stay portable — if a future deployment ever needs real OAuth there, the swap is mechanical.

- **Schema:** `portal_auth` (migration `0008_v080_portal_auth`). Four tables — `identity` (email-keyed, FK-linkable to a `customer_id`), `login_token` (OTP / magic-link / step-up, all hashed), `session` (server-side; cookie carries the id only), `login_attempt` (append-only audit + rate-limit substrate).
- **Library:** `packages/bss-portal-auth/`. Pure Python (no HTTP service of its own). Public surface: `start_email_login` / `verify_email_login` / `current_session` / `rotate_if_due` / `revoke_session` / `link_to_customer` / `start_step_up` / `verify_step_up` / `consume_step_up_token` / `validate_pepper_present`. Tokens HMAC-SHA-256-keyed by `BSS_PORTAL_TOKEN_PEPPER` (≥32 chars; sentinel + length validated at portal startup). Comparison via `hmac.compare_digest`.
- **Middleware:** `bss_self_serve.middleware.PortalSessionMiddleware` (pure ASGI, SSE-safe). Sits between request-id middleware and route resolution. Reads the `bss_portal_session` cookie off the ASGI scope, resolves to (session, identity), attaches `request.state.session` / `.identity` / `.customer_id`, rotates session id past TTL/2 with Set-Cookie writeback. The ONLY path that touches the cookie header.
- **Public route allowlist:** `/welcome`, `/plans`, `/auth/*`, `/static/*`, `/portal-ui/static/*`. Adding a public route requires both an entry in `bss_self_serve.security.PUBLIC_*` and a test.
- **Step-up auth:** `requires_step_up(action_label)` dep consumes a one-shot grant carried via `X-BSS-StepUp-Token` header / `step_up_token` form field / `bss_portal_step_up` cookie (60s TTL, set by `POST /auth/step-up`). Tokens are scoped to a single `action_label` — a grant minted for `subscription.terminate` cannot satisfy a `payment.remove_method` route.
- **Email delivery:** pluggable. v0.8 ships `LoggingEmailAdapter` (writes plaintext OTPs + magic links to `BSS_PORTAL_DEV_MAILBOX_PATH` for dev / staging; the file is the only place the plaintext lives outside the customer's inbox) and `NoopEmailAdapter` (tests). `SmtpEmailAdapter` is reserved for v1.0 and raises at construction.
- **Account-first signup funnel:** the entry points (`/signup/{plan}`, `/signup/{plan}/msisdn`, `POST /signup`, `/signup/{plan}/progress`) are gated on `Depends(requires_verified_email)`. **(v0.11+)** The signup chain writes directly via `bss-clients` from route handlers — `customer.create` runs in the POST handler, then `customer.attest_kyc` + `payment.add_card` + `com.create_order` in their own routes — and the route handler calls `link_to_customer` the moment `customer.create` returns a CUST-* id, atomically binding the verified identity to the customer record. A returning visitor under the same email reuses the same `(identity, customer)` pair.
- **Login-gated `/`:** the v0.4 anonymous landing moved to `/plans`. `/` is now the dashboard — empty for verified-but-unlinked identities, lines + balances + state-aware CTAs for linked (v0.10).
- **Topology placement:** for the self-serve portal (v0.11+ — direct-write signup + post-login self-serve),
  ```
  request -> RequestIdMiddleware -> PortalSessionMiddleware -> route -> bss-clients (NamedTokenAuthProvider) -> services
  ```
  Chat (v0.12) is the one self-serve route that goes `route -> astream_once -> bss-clients` (with `tool_filter="customer_self_serve"` narrowing the LLM-visible surface — see "Chat scoping" below). Operator cockpit (v0.13): the cockpit's `/cockpit/{id}/events` SSE route is the only orchestrator-mediated route on port 9002 — drives `astream_once(tool_filter="operator_cockpit", service_identity="operator_cockpit")`. No inbound middleware (perimeter trust). The customer-side gate stays portable; staff side is gone by design.
- **Runbook:** `docs/runbooks/portal-auth.md` (token pepper generation + rotation, dev mailbox tail, brute-force investigation, unverified-identity cleanup).

The **Inventory sub-domain** (MSISDN pool + eSIM profile pool) lives inside the CRM service on port 8002, mounted under `/inventory-api/v1/...`. It has its own schema (`inventory`), repositories, policies, and HTTP endpoints — just no separate container. SOM and Subscription call it via `bss-clients` as if it were a distinct service. If it outgrows CRM, extraction to an 11th container is mechanical because the boundary is already enforced.

**Why 9 containers, not 10:** keeping inventory inside CRM for v0.1 reduces one network hop in the critical activation path and saves ~150MB of RAM. Domain boundary is still clean — inventory has its own schema, repositories, policies, and tool surface. See DECISIONS.md "Inventory domain hosted inside CRM service (v0.1)" for the rationale.

### Operator cockpit (v0.13)

v0.13 retires the v0.5 CSR portal pattern (login + 360 view + 4
HTMX auto-refresh partials + ask-form). The CLI REPL (`bss`)
becomes the canonical operator cockpit; the browser at port 9002
becomes a thin veneer over the same Postgres-backed
`Conversation` store. Both surfaces drive `astream_once` with
identical parameters; the only difference is the channel name
(`"cli"` vs `"portal-csr"`) and the Rich vs HTMX presentation.

```
   ┌───────── operator workstation ─────────┐
   │                                        │
   │  $ bss [--session SES-...] [--new]     │  ← REPL canonical
   │      │                                 │
   │      ▼                                 │
   │  Conversation.{open,resume,append_*}   │
   │      ▼                                 │
   │  astream_once(transcript=,             │
   │               actor=settings.actor,    │
   │               channel="cli",           │
   │               service_identity=        │
   │                 "operator_cockpit",    │
   │               tool_filter=             │
   │                 "operator_cockpit",    │
   │               system_prompt=           │
   │                 build_cockpit_prompt(  │
   │                   operator_md, ...))   │
   │                                        │
   │  http://localhost:9002/cockpit/<id>    │  ← browser veneer
   │      │                                 │
   │      ▼                                 │
   │  /cockpit/{id}/events SSE              │
   │  (same astream_once, channel=          │
   │   "portal-csr")                        │
   └────────────────┬───────────────────────┘
                    │
                    ▼
   ┌────────────────────────────────────────┐
   │ packages/bss-cockpit                   │
   │   • Conversation + ConversationStore   │
   │     (cockpit.session/message/pending_  │
   │      destructive — alembic 0014)       │
   │   • config: OPERATOR.md + settings.toml│
   │     mtime hot-reload + autobootstrap   │
   │   • prompts: build_cockpit_prompt      │
   │     (operator persona prepended +      │
   │      code-defined invariants +         │
   │      focus + pending_destructive)      │
   └────────────────┬───────────────────────┘
                    │
                    ▼
   audit.domain_event(actor=<settings.actor>,
                     service_identity="operator_cockpit",
                     channel="cli"|"portal-csr")
```

**Key components.**

- **`packages/bss-cockpit/`** — new workspace package owning the
  Conversation store, the `OPERATOR.md` + `settings.toml` loader
  (with mtime hot-reload + first-run autobootstrap from embedded
  defaults — needed for container deploys with no `.template`
  files on disk), and the prompt builder. Public API:
  `Conversation`, `ConversationStore`, `ConversationSummary`,
  `PendingDestructive`, `configure_store`, `current`,
  `build_cockpit_prompt`, `write_operator_md`, `write_settings_toml`.
- **`cockpit` schema (alembic 0014).** Three tables. `session`
  carries `actor` / `customer_focus` / `allow_destructive` /
  `state` / `label` plus `tenant_id` (DEFAULT). `message` is the
  append-only conversation log (role / content / `tool_calls_json`).
  `pending_destructive` is the at-most-one-per-session in-flight
  propose row, consumed by `/confirm`.
- **`operator_cockpit` tool profile.** Full registry minus
  `*.mine` / `*_for_me` wrappers (those exist for prompt-injection
  containment on the customer chat side; the operator binds via
  `actor=settings.actor` and has no ownership scoping). 82 tools
  on registration; coverage drift caught by
  `validate_profiles()` at orchestrator boot.
- **`BSS_OPERATOR_COCKPIT_API_TOKEN`.** Third named token at the
  v0.9 perimeter. TokenMap auto-derives identity
  `"operator_cockpit"` from the env-var name. Cockpit-driven
  downstream calls stamp `audit.domain_event.service_identity`
  cleanly.
- **No login.** No `OperatorSessionStore`, no `require_operator`
  dependency, no `BSSApiTokenMiddleware` on the portal's inbound
  HTTP. The cockpit runs single-operator-by-design behind a
  secure perimeter (Tailscale, VPN, local LAN). `actor` from
  `.bss-cli/settings.toml` (descriptive, not verified).
  DECISIONS 2026-05-01 documents the rationale.

**Cross-surface round trip.** REPL writes turn → browser reads
it → browser writes turn → REPL `--session SES-...` resumes →
REPL sees the browser's turn. The
`portals/csr/tests/test_cross_surface_session.py` parameter-
ized-x3 test asserts this on every PR run.

**Slash-command parity.** The REPL surface ships 11 slash
commands: `/sessions`, `/new [LABEL]`, `/switch SES-...`,
`/reset`, `/focus CUST-NNN`, `/focus clear`, `/360 [CUST-NNN]`,
`/confirm`, `/config edit`, `/operator edit`, `/help`, `/exit`.
The browser exposes equivalent affordances via the cockpit
thread template (focus form, reset button, /confirm button,
back to sessions index, link to `/settings`). Drift between the
two is a doctrine bug to fix in the next sprint.

**WebUI `/settings`.** Two textareas (`OPERATOR.md` + `settings.toml`)
backed by `bss_cockpit.write_operator_md` /
`write_settings_toml`. Validation failures preserve the operator's
draft and echo the parser/Pydantic diagnostic in the page. Last
good view stays in effect on parse failure (mtime hot-reload only
swaps on a successful parse).

**Autonomy mode (v1.5).** Compound actions ("register this customer
then create their order", "investigate CASE-042 and tell me what's
going on") are unlocked in v1.5 by softening the v0.19 anti-
hallucination "Done." rule + adding an `ITERATIVE FLOW` block to the
operator system prompt. Two operator-visible knobs control how
chatty the loop is with `/confirm`:

- `BSS_REPL_LLM_AUTONOMY=granular` (default) — every destructive
  step in a compound action propose-then-`/confirm`s separately.
  Per-step operator control even after the first authorisation.
- `BSS_REPL_LLM_AUTONOMY=batched` — the FIRST destructive step in a
  `/confirm`-resumed loop gates; subsequent destructive steps in the
  same loop execute autonomously. One `/confirm` authorises the
  plan; the loop runs to completion.

Read once at process boot via `bss_orchestrator.autonomy.read_autonomy_mode()`
(fail-closed on a bad value, same shape as `BSS_API_TOKEN=changeme`)
and cached on `app.state.autonomy_mode` (cockpit portal) or the
REPL's module-level `_AUTONOMY_MODE`. Scope is per-process — a
per-session `/autonomy {granular,batched}` slash command is
deferred to v1.5.1. The autonomy mode flows through
`astream_once(autonomy_mode=...)` → `build_graph` → `build_tools` →
the per-graph `LoopState` that every destructive wrapper observes.

Two safety rails sit alongside autonomy:

- **3-strike loop bail** (`MAX_CONSECUTIVE_TOOL_FAILURES=3` in
  `bss_orchestrator.session`). When three consecutive tool results
  are failure-shaped (real exceptions OR structured
  `POLICY_VIOLATION` / `DESTRUCTIVE_OPERATION_BLOCKED` /
  `CLIENT_ERROR`), the stream terminates with a structured
  `AgentEventError` and the cockpit renders a "couldn't recover"
  panel — catches Gemma thrash without an unbounded loop.
- **Cockpit chrome filter** (`bss_cockpit.chrome_filter`). When
  `Conversation.transcript_text()` rehydrates prior turns into the
  LLM's context, cockpit-emitted chrome (the route error fallback,
  the empty-final recovery bubble, the citation-guard fallback,
  totally empty AIMessages) is stripped before the LLM sees it.
  Without the filter the LLM mimics the placeholder strings
  instead of doing the work. Inventory-locked by a unit test so a
  new fallback bubble added without a matching prefix surfaces in
  code review.

The destructive-tool list (`DESTRUCTIVE_TOOLS` in
`bss_orchestrator.safety`) is unchanged in v1.5. Autonomy controls
*how many* `/confirm`s a compound action needs, not *which* tools
require one.

### Chat scoping (self-serve, v0.12)

The chat surface is the only orchestrator-mediated route in the
self-serve portal. v0.12 narrows it from "the LLM has the full
tool registry" to "the LLM sees a curated `customer_self_serve`
profile, every tool ownership-bound to the logged-in customer,
with rate + cost caps and an explicit escalation path for the
five categories AI must not handle alone."

```
browser /chat
   │
   ▼
PortalSessionMiddleware  →  request.state.customer_id
   │
   ▼
routes/chat.py
   │ POST /chat/message  → check_caps(customer_id) → blocked?
   │   │                                      └── 303 cap-tripped banner
   │   └── 303 /chat?session=<sid>           (no LLM invocation)
   │
   │ GET /chat/events/{sid}
   │   ├── fetch customer + primary subscription
   │   ├── build customer_chat system prompt
   │   └── astream_once(
   │           actor=customer_id,
   │           channel="portal-chat",
   │           service_identity="portal_self_serve",
   │           tool_filter="customer_self_serve",
   │           system_prompt=<rendered>,
   │           transcript="User: ...\n",
   │       )
   │
   ▼
orchestrator
   │
   ├── auth_context.set_actor(customer_id)
   │
   ├── build_graph(tool_filter="customer_self_serve",
   │               allow_destructive=True)        → 16 tools
   │       │
   │       ├── catalog.list_vas / list_active_offerings / get_offering   (public)
   │       ├── subscription.{list,get,get_balance,get_lpa}_mine          (read .mine)
   │       ├── usage.history_mine                                        (read .mine)
   │       ├── customer.get_mine                                         (read .mine)
   │       ├── payment.{method_list,charge_history}_mine                 (read .mine)
   │       ├── vas.purchase_for_me                                       (write .mine)
   │       ├── subscription.{schedule_plan_change,cancel_pending_plan_change,terminate}_mine
   │       └── case.open_for_me                                          (escalation)
   │
   ├── after each ToolMessage:
   │       assert_owned_output(tool_name, result, actor)
   │           - mismatch → AgentEventError + record_violation + stream end
   │
   ├── at stream end:
   │       AgentEventTurnUsage(prompt_tok, completion_tok, model)
   │           - chat route reads → chat_caps.record_chat_turn
   │           - upserts audit.chat_usage row + hourly window
   │
   └── case.open_for_me path:
           hash_(transcript) → store in audit.chat_transcript
           → crm.open_case(chat_transcript_hash=hash_)
           CSR sees the transcript on /case/{id} via case.show_transcript_for
```

**Three layers of defence-in-depth, primary boundary first:**

1. **Server-side policies.** Same as every other write — the
   subscription service rejects a cross-customer terminate
   regardless of whether the chat surface or the CLI initiated.
2. **Wrapper pre-check.** `*.mine` wrappers fetch the resource
   (subscription/case) and assert `customerId == actor` before
   delegating. Produces a uniform `policy.<tool>.not_owned_by_actor`
   observation across every tool so prompt-injection attempts get
   the same response shape.
3. **Output trip-wire.** `assert_owned_output` runs after every
   non-error tool result against `OWNERSHIP_PATHS`. A trip is a
   P0 — server-side policy missed a case. The check exists to
   fail loudly the day that happens, not to substitute for
   getting policies right.

**Caps:** per-customer hourly rate (in-memory sliding window,
default 20/h) + per-customer monthly cost ceiling (DB-backed via
`audit.chat_usage`, default 200 cents). Both **fail closed** on
any error — `chat_caps.check_caps` catches DB exceptions and
returns `CapStatus(allowed=False, reason="cap_check_failed")` so
the route refuses without invoking the LLM.

### Deployability matrix

| Concern | v0.11 | v0.12 | v0.15 | v0.17 | v0.18 | v1.0 |
|---|---|---|---|---|---|---|
| Customer signup (KYC) | ✅ direct + mocked attestation | ✅ unchanged | ✅ Didit live (channel-layer) + prebaked dev path | ✅ unchanged | ✅ unchanged | ⏳ real Singpass |
| Card on file | ✅ mock tokenizer | ✅ unchanged | ✅ unchanged | ✅ Stripe Checkout (v0.16) live | ✅ unchanged | ⏳ unchanged |
| eSIM provisioning | ✅ provisioning-sim | ✅ unchanged | ✅ Protocol seam (sim only; real providers v0.16+) | ✅ unchanged | ✅ unchanged | ⏳ real SM-DP+ |
| Customer chat | _absent_ | ✅ scoped + capped + escalation | ✅ unchanged shape | ✅ unchanged shape | ✅ unchanged shape | ✅ unchanged shape |
| Chat ownership trip-wire | _absent_ | ✅ defence-in-depth | ✅ unchanged shape | ✅ unchanged shape | ✅ unchanged shape | ✅ unchanged shape |
| 14-day soak | _absent_ | ✅ frozen-clock 100×14 | ✅ unchanged (prebaked KYC path preserved) | ✅ unchanged (roaming usage path covered by hero) | ✅ unchanged (auto-renewal exercised by tick-now sweep) | ⏳ public soak with real cohort |
| Per-principal RBAC (staff) | _absent_ | _absent_ | _absent_ | _absent_ | _absent_ | _retired in v0.13 — operator trust is perimeter-based; DECISIONS 2026-05-01_ |
| MNP (port-in / port-out) | _absent_ | _absent_ | _absent_ | ✅ operator-driven `crm.port_request` aggregate | ✅ unchanged shape | ⏳ unchanged shape (donor-carrier integration channel-layer) |
| MSISDN pool replenishment | _absent_ | _absent_ | _absent_ | ✅ operator CLI + cockpit + low-watermark event | ✅ unchanged | ⏳ unchanged |
| Roaming as a product | _absent_ | _absent_ | _absent_ | ✅ bundled `data_roaming` MB + VAS_ROAMING_1GB top-up | ✅ unchanged | ⏳ per-country tariff (post-v0.x) |
| Subscription renewal | ✅ manual only (CLI / cockpit / scenario) | ✅ unchanged | ✅ unchanged | ✅ unchanged | ✅ in-process tick loop in subscription lifespan; multi-replica safe via FOR UPDATE SKIP LOCKED | ✅ unchanged shape |

### v0.15 KYC — Didit (channel-layer)

The KYC verification flow lives in the portal (channel-layer doctrine
per CLAUDE.md "Scope boundaries: eKYC"). BSS receives a verification
*receipt* (last4 + hash + corroboration_id), never raw PII.

```
┌──── browser ────┐    ┌──── portal-self-serve (9001) ────────────┐    ┌── BSS-CLI ──┐
│ /signup/step/kyc├──▶│ kyc_adapter.initiate(email, return_url)   │    │             │
│                 │   │   → DiditKycAdapter → POST /v2/session/   │    │             │
│  HX-Redirect ◀──┼───┤                                            │    │             │
│       │         │   │                                            │    │             │
│       ▼         │   │                                            │    │             │
│ Didit hosted UI │   │                                            │    │             │
│ (doc + liveness)│   │                                            │    │             │
│       │         │   │                                            │    │             │
│       ├─────────┼──▶│ POST /webhooks/didit  ──HMAC verify──▶     │    │             │
│       │         │   │   → integrations.webhook_event             │    │             │
│       │         │   │   → integrations.kyc_webhook_corroboration │    │             │
│       │         │   │       (trust anchor, FK on session_id)     │    │             │
│       ▼         │   │                                            │    │             │
│ /signup/step/   │   │ kyc_adapter.fetch_attestation(session_id) │    │             │
│ kyc/callback    │──▶│   → polls corroboration row (10s timeout) │    │             │
│                 │   │   → reduces PII: last4 + hash + drop rest │    │             │
│                 │   │   → crm.attest_kyc(corroboration_id, …)   │───▶│ check_attest│
│                 │   │                                            │    │ ation_      │
│                 │   │                                            │    │ signature   │
│                 │   │                                            │    │ (verifies   │
│                 │   │                                            │    │ corrobora-  │
│                 │   │                                            │    │ tion row    │
│                 │   │                                            │    │ exists,     │
│                 │   │                                            │    │ Approved,   │
│                 │   │                                            │    │ <30 min)    │
└─────────────────┘    └────────────────────────────────────────────┘    └─────────────┘
```

The `prebaked` adapter loops the customer back to the callback
without external hops — used by the v0.12 14-day soak corpus and
hero scenarios. Selection via `BSS_PORTAL_KYC_PROVIDER`. Full
doctrine + alternatives in DECISIONS.md (2026-05-02 entries).

### v0.16 Payment — Stripe (service-layer)

The payment provider seam lives in the payment service itself
(`services/payment/app/domain/`), not the channel layer — payment is
a back-office concern, not a customer-input concern. The portal uses
**Stripe Checkout** (full-page redirect to Stripe-hosted card form);
the customer's PAN goes directly to Stripe's domain — BSS only ever
sees the resulting `pm_*` id (DECISIONS 2026-05-03 — Checkout over
Elements, browser compatibility).

```
┌──── browser ────┐    ┌──── portal-self-serve ─────────┐    ┌── payment service (8003) ──────┐    ┌── Stripe ──┐
│                 │    │                                 │    │                                 │    │            │
│ /signup/step/cof│    │ render "Continue to Stripe →"   │    │                                 │    │            │
│ pending_cof step│◀───┤   button (no Stripe.js)         │    │                                 │    │            │
│                 │    │                                 │    │                                 │    │            │
│ click button ──▶│    │ POST /signup/step/cof/checkout-init                                    │    │            │
│                 │    │   ensure cus_* via bss-clients ─────▶ POST /admin-api/.../ensure       │    │            │
│                 │    │   ◀───── cus_* ──────────────────────│  StripeTokenizerAdapter         │    │            │
│                 │    │                                 │    │  .ensure_customer ─────────────▶│ Customer.  │
│                 │    │                                 │    │  ◀───── cus_* ──────────────────│  create   │
│                 │    │                                 │    │  (cached in payment.customer)   │    │            │
│                 │    │   stripe.checkout.Session.create ──────────────────────────────────────────▶│ Checkout │
│                 │    │     (mode=setup, customer=cus_*)│    │                                 │ Session    │
│                 │    │   ◀──── session.url (cs_*) ────────────────────────────────────────────────│  .create  │
│                 │    │                                 │    │                                 │    │            │
│ ◀── 303 redirect to checkout.stripe.com/c/pay/cs_* ────┤    │                                 │    │            │
│                 │    │                                 │    │                                 │    │            │
│ Customer enters card on Stripe-hosted page             │    │                                 │    │            │
│ (Stripe's domain, no iframe, no JS we control)         │    │                                 │    │            │
│                 │    │                                 │    │                                 │    │            │
│ ◀── 303 redirect back to portal: ──────────────────────────────────────────────────────────────────│            │
│   /signup/step/cof/checkout-return?cs_id=cs_*          │    │                                 │    │            │
│                 │    │                                 │    │                                 │    │            │
│ GET checkout-   │    │ stripe.checkout.Session.retrieve                                        │    │            │
│   return ──────▶│    │   (expand=setup_intent) ─────────────────────────────────────────────────▶│ retrieve  │
│                 │    │ ◀──── setup_intent.payment_method (pm_*) ──────────────────────────────────│            │
│                 │    │                                 │    │                                 │    │            │
│                 │    │ POST /tmf-api/.../paymentMethod │    │ PaymentMethodService.register   │    │            │
│                 │    │   (pm_*, token_provider=stripe) ───▶                                    │    │            │
│                 │    │                                 │    │ (cus_* already attached at      │    │            │
│                 │    │                                 │    │  Checkout time, so no extra     │    │            │
│                 │    │                                 │    │  attach call needed)            │    │            │
│                 │    │                                 │    │                                 │    │            │
│ ◀── 303 to /signup/PLAN_M/progress (state=pending_order)    │                                 │    │            │
│                 │    │                                 │    │                                 │    │            │
│                 │    │                                 │    │  --- at renewal time ---        │    │            │
│                 │    │                                 │    │  PaymentService.charge          │    │            │
│                 │    │                                 │    │   → tokenizer.charge(pm_*, …)──▶│ PaymentInt │
│                 │    │                                 │    │   (idempotency_key=ATT-{id}-r0) │ ent.create │
│                 │    │                                 │    │  ◀───── status, pi_*, ──────────│ confirm=T  │
│                 │    │                                 │    │     decline_code                │            │
│                 │    │                                 │    │   → row + audit.domain_event    │            │
│                 │    │                                 │    │                                 │            │
│                 │    │                                 │    │  POST /webhooks/stripe ◀────────────────── webhook
│                 │    │                                 │    │   verify HMAC (Stripe scheme)   │  charge.* │
│                 │    │                                 │    │   → integrations.webhook_event  │  refund.* │
│                 │    │                                 │    │   → reconcile or drift event    │  dispute  │
└─────────────────┘    └─────────────────────────────────┘    └─────────────────────────────────┘    └────────────┘
```

The `mock` adapter (default, used by hero scenarios) preserves every
v0.1 `tok_FAIL_*`/`tok_DECLINE_*` test affordance and skips Stripe
entirely. Selection via `BSS_PAYMENT_PROVIDER`. Five startup guards in
`select_tokenizer` fail-fast on misconfig (missing creds, sk_test_*
in production, ALLOW_TEST_CARD_REUSE + sk_live_*, etc.).

The webhook is **secondary source of truth** — the synchronous Stripe
response from `charge` writes the `payment_attempt.status`; webhooks
reconcile and emit `payment.attempt_state_drift` on contradiction
without overwriting the row. **Chargebacks (`charge.dispute.created`)
and out-of-band refunds (`charge.refunded`) are record-only** —
emit `payment.dispute_opened` / `payment.refunded` for the cockpit;
no auto-action (motto #1). Full doctrine + alternatives in
DECISIONS.md (2026-05-03 entries).

The cutover playbook (mock → stripe) lives at
`docs/runbooks/stripe-cutover.md`. Lazy-fail (next charge against any
mock-token row raises `payment.charge.token_provider_matches_active`)
is the default; `bss payment cutover --invalidate-mock-tokens` is the
proactive path that emits one `payment_method.cutover_invalidated`
event per row so the v0.14 Resend email-template flow can notify each
customer to re-add their card before the env-var flip.

### v0.17 Telco hygiene — MNP, MSISDN replenishment, roaming

Three real-telco gaps closed without new doctrine pillars or new external integrations. Single Alembic migration (`0019`).

**MNP (port-in / port-out, operator-only).** New `crm.port_request` aggregate distinct from `crm.case` (port requests have a fixed FSM + specific data shape; CLAUDE.md v0.17+ anti-pattern bars Case overload). FSM: `requested → validated → completed | rejected`. Approve dispatches:

- **Port-in:** donor MSISDN gets seeded into `inventory.msisdn_pool` (status `assigned` if `target_subscription_id` is set, else `available`) via `INSERT … ON CONFLICT DO NOTHING`. Emits `inventory.msisdn.seeded_from_port_in`.
- **Port-out:** donor MSISDN flips to terminal `ported_out` (`quarantine_until='9999-12-31'` so reserve-next never selects it again — the predicate is `status='available'`); the target subscription is then terminated via `SubscriptionClient.terminate(release_inventory=False)` (new v0.17 kwarg) so the MSISDN release path is skipped. eSIM still recycles. Emits `inventory.msisdn.ported_out` + `subscription.terminated`.

Operator surfaces: REPL `/ports` slash command (`list`, `approve PORT-NNN`, `reject PORT-NNN <reason>`) + cockpit tools `port_request.{list,get,create,approve,reject}`. Registered in `operator_cockpit` profile only — exposing port-request writes to `customer_self_serve` is a doctrine bug caught by `validate_profiles()`.

**MSISDN replenishment.** `bss inventory msisdn add-range <prefix> <count>` (CLI + cockpit tool) bulk-inserts `count` MSISDNs starting at `{prefix}{0:04d}`. Idempotent on overlap. Post-commit fire-and-forget hook on every successful reserve emits `inventory.msisdn.pool_low` when available count drops to or below `BSS_INVENTORY_MSISDN_POOL_LOW_THRESHOLD` (default 50).

**Roaming as a product.** New `data_roaming` allowance type. Plans seed: PLAN_S=0, PLAN_M=500 mb, PLAN_L=2048 mb. New `VAS_ROAMING_1GB` top-up (8 SGD, 1024 mb, no expiry). Pipeline:

```
mediation                rating consumer                 subscription
─────────                ──────────────                  ────────────
UsageEvent               post-rate override:             handle_usage_rated:
+ roaming_indicator ──▶  if roamingIndicator and    ──▶  if allowance_type == 'data_roaming':
  (boolean,                 result.allowance_type            check_roaming_balance_required(...)
   server_default              == 'data':                    └─ rejects when balance row missing
   false)                  validate offering carries           OR remaining ≤ 0
                              data_roaming                     subscription stays `active`,
                           override allowance_type             home `data` untouched
                              to 'data_roaming'                emit usage.rejected
                           (else publish usage.rejected
                            with rating.no_roaming_allowance)
```

Doctrine v0.17+:

- Roaming is a per-event **attribute** (`roaming_indicator: bool`), not a new `event_type`. Mediation's `VALID_EVENT_TYPES` set stays `{data, voice, voice_minutes, sms}`.
- The pure `rate_usage` function is unchanged — routing happens in the consumer, after rate_usage returns. Doctrine guard: `data_roaming` must NOT appear in `services/rating/app/domain/rating.py`.
- `data_roaming` is **additive**, never primary. `is_exhausted()` continues to consider only `primary_type='data'`. Roaming-balance exhaustion blocks roaming usage but does not block the subscription itself.
- The portal line_card filter hides the Roaming bar when `total=0 AND remaining=0` so PLAN_S users don't see a stranded "Roaming 0/0".

`purchase_vas` now materializes a missing balance row from the VAS spec (generic fix, not roaming-specific) — unblocks any future allowance type without code change.

### v0.18 Automated subscription-renewal worker

The v0.7 renewal logic (`subscription_service.renew()` — handles the period boundary, the price snapshot, the pending plan-change pivot, the payment-decline → block path, and every audit event) had no automatic trigger until v0.18. Manual paths (CLI `bss subscription renew`, cockpit `subscription.renew_now`, scenario action) were the only callers — operator escape hatches, not the production path.

v0.18 adds an in-process tick loop attached to the subscription service's lifespan. **The worker calls `service.renew(sub_id)` and nothing else** — no logic duplication. The only new logic is *triggering*.

```
                                      v0.18 renewal worker
                                      ────────────────────
              ┌────────── subscription service container ──────────┐
              │                                                     │
   lifespan ──┤ if BSS_RENEWAL_TICK_SECONDS > 0:                   │
              │     asyncio.create_task(_renewal_tick_loop)         │
              │                                                     │
              │  ┌──────────── tick loop (every 60s) ─────────────┐ │
              │  │                                                 │ │
              │  │  txn 1 (single batch, 1 commit):               │ │
              │  │    SELECT id FROM subscription                  │ │
              │  │    WHERE state = 'active'                       │ │
              │  │      AND next_renewal_at <= clock_now()         │ │
              │  │      AND (last_renewal_attempted_at IS NULL     │ │
              │  │           OR last_renewal_attempted_at          │ │
              │  │              < next_renewal_at)                 │ │
              │  │    LIMIT 100 FOR UPDATE SKIP LOCKED             │ │
              │  │    ─────────────────────────────────────────    │ │
              │  │    UPDATE subscription                          │ │
              │  │    SET last_renewal_attempted_at = clock_now()  │ │
              │  │    WHERE id = ANY(:ids)                         │ │
              │  │    COMMIT — releases SKIP LOCKED row locks      │ │
              │  │       (peer replicas now see marked rows)       │ │
              │  │                                                 │ │
              │  │  txn 2..N+1 (one per id, fresh session each):  │ │
              │  │    auth_context.push(actor=                     │ │
              │  │       'system:renewal_worker', channel='system')│ │
              │  │    try:                                         │ │
              │  │      await service.renew(sub_id)                │ │
              │  │    except PolicyViolation: log warn, continue   │ │
              │  │    except Exception:        log error, continue │ │
              │  │    finally: auth_context.pop(token)             │ │
              │  │                                                 │ │
              │  │  same shape for _sweep_skipped (state='blocked')│ │
              │  │  → emits `subscription.renewal_skipped` event   │ │
              │  └─────────────────────────────────────────────────┘ │
              │                                                     │
              └─────────────────────────────────────────────────────┘
```

Doctrine v0.18+:

- **Trigger lives only in the subscription lifespan.** No sibling scheduler container, no cron, no Celery beat, no external poller. The whole point of `FOR UPDATE SKIP LOCKED` is that "every replica runs the loop" is multi-replica safe by construction; running the trigger elsewhere defeats the design.
- **Worker NEVER duplicates `renew()` logic.** `service.renew(sub_id)` and nothing else. Future renewal extensions land in `renew()` once and the worker picks them up for free.
- **Mark-before-dispatch ordering.** `last_renewal_attempted_at` is committed by the SELECT-txn BEFORE the row lock releases, so a peer replica's next sweep sees the mark the moment the lock is gone. Reverse the order and two replicas can each grab the same row → double charge.
- **Per-id session for dispatch.** A single subscription's failure does not poison the rest of the batch.
- **ContextVar reset in `finally`.** Worker is one long-lived asyncio Task; values would leak across iterations without the explicit Token-pattern (`auth_context.push()` / `pop()`).

`BSS_RENEWAL_TICK_SECONDS=0` disables the worker (e.g. for unit tests, or for deployments running multiple subscription replicas behind an external orchestrator). The admin endpoint `POST /admin-api/v1/renewal/tick-now` (gated by `BSS_ALLOW_ADMIN_RESET`) drives one deterministic sweep — used by the v0.18 hero scenario after `clock.advance` to avoid waiting 60 wall-seconds for the natural tick. Production deployments keep the flag false.

The worker runs three sweeps per tick:

1. `_sweep_due` — active subs whose `next_renewal_at` has passed → dispatch `service.renew()`. Audit attribution: `actor=system:renewal_worker`.
2. `_sweep_skipped` — blocked subs whose boundary has passed → emit `subscription.renewal_skipped` (no charge). Cockpit signal.
3. `_sweep_upcoming_renewal_reminder` — active subs whose `next_renewal_at` falls inside `BSS_RENEWAL_REMINDER_LOOKAHEAD_SECONDS` (default 24h) → look up customer email via CRM, send a reminder email via the portal `EmailAdapter`, deduped via the `subscription.renewal_reminder_sent_at` column (migration `0021`). The email reuses the OTP visual vibe (dark theme, bordered green highlight block) but carries the renewal amount instead of an OTP. Set lookahead to 0 to disable just the reminder sweep without disabling renewal.

### Note on billing in v0.1

v0.1 ships **without a billing service**. Phase 0 planned one as service #9 (TMF678, port 8009), and the Phase 2 initial migration created the `billing` schema with two tables (`billing_account`, `customer_bill`) — but no phase actually built the service layer. v0.1.1 formally defers billing to v0.2, where it will be reintroduced as a **read-only view layer over `payment.payment_attempt`**: receipt aggregation, statement generation, TMF678 `/customerBill` endpoints. No dunning, no credit extension, no formal invoice generation — bundled-prepaid doesn't need them, since charges happen synchronously at activation / renewal / VAS purchase and are already recorded on `payment.payment_attempt`. The `billing` schema and its tables remain in the migration so v0.2 is purely additive. Port 8009 is reserved. See `DECISIONS.md` 2026-04-13 for the deferral rationale and the scope note separating the billing **service** from "billing" as CRM customer-support **vocabulary**.

## Container structure

### Default deployment: BYOI (bring your own infrastructure)

**BSS-CLI has been developed in BYOI mode from Phase 1 onwards.** The default `docker-compose.yml` contains only the 9 BSS services and assumes PostgreSQL 16 (with the `pgvector` extension installed; v0.20+ requirement) and RabbitMQ 3.13 are reachable via env-configured connection strings. Most operators already have managed Postgres (RDS, Cloud SQL) and managed MQ (Amazon MQ, CloudAMQP), so bundling an unused Postgres container would be wasteful. Managed Postgres offerings (RDS, Cloud SQL, Aiven, Neon) ship pgvector as a switch-on-able extension; run `CREATE EXTENSION IF NOT EXISTS vector` once per BSS database before `make migrate`.

```yaml
# docker-compose.yml
services:
  catalog:          { build: ./services/catalog,         env_file: .env, ports: ["8001:8000"] }
  crm:              { build: ./services/crm,             env_file: .env, ports: ["8002:8000"] }
  payment:          { build: ./services/payment,         env_file: .env, ports: ["8003:8000"] }
  com:              { build: ./services/com,             env_file: .env, ports: ["8004:8000"] }
  som:              { build: ./services/som,             env_file: .env, ports: ["8005:8000"] }
  subscription:     { build: ./services/subscription,    env_file: .env, ports: ["8006:8000"] }
  mediation:        { build: ./services/mediation,       env_file: .env, ports: ["8007:8000"] }
  rating:           { build: ./services/rating,          env_file: .env, ports: ["8008:8000"] }
  # billing: port 8009 reserved for v0.2 (see DECISIONS.md 2026-04-13)
  provisioning-sim: { build: ./services/provisioning-sim, env_file: .env, ports: ["8010:8000"] }
```

Each service reads `BSS_DB_URL` and `BSS_MQ_URL` from env. No assumptions about where Postgres or RabbitMQ live.

### Optional infra compose: all-in-one

`docker-compose.infra.yml` brings up Postgres, RabbitMQ, and Jaeger in containers for operators who prefer a single-command bring-up. This is **not the primary development mode** — it exists as a bring-up convenience for new contributors and for the README quickstart.

```yaml
# docker-compose.infra.yml
services:
  postgres:
    image: pgvector/pgvector:pg16   # v0.20+ — was postgres:16-alpine; pgvector required
    environment:
      POSTGRES_USER: bss
      POSTGRES_PASSWORD: bss
      POSTGRES_DB: bss
    ports: ["5432:5432"]
    volumes: [postgres_data:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "bss"]
      interval: 10s

  rabbitmq:
    image: rabbitmq:3.13-management-alpine
    ports: ["5672:5672", "15672:15672"]
    volumes: [rabbitmq_data:/var/lib/rabbitmq]

  jaeger:
    image: jaegertracing/all-in-one:1.65.0
    environment:
      COLLECTOR_OTLP_ENABLED: "true"
    ports: ["4317:4317", "4318:4318", "16686:16686"]

volumes:
  postgres_data:
  rabbitmq_data:
```

Usage:

```bash
# BYOI (default, development mode)
docker compose up -d

# All-in-one (dev/demo, new contributor quickstart)
docker compose -f docker-compose.yml -f docker-compose.infra.yml up -d
```

### Compose profiles for incremental bringup

For development on slow machines, profiles allow partial stacks:

```yaml
profiles:
  minimal:  [catalog, crm, payment]
  core:     [catalog, crm, payment, com, som, subscription, provisioning-sim]
  full:     [catalog, crm, payment, com, som, subscription, mediation, rating, provisioning-sim]
```

```bash
docker compose --profile core up       # Phase 3-7 development
docker compose --profile full up       # Phase 8+ and scenarios
```

### Per-service Dockerfile pattern

**Current implementation (Phase 3/4 expedient):** each service has its own `Dockerfile` that rewrites the workspace reference to a direct path before running `uv pip install`. This is a workaround for uv workspace resolution inside Docker build contexts.

```dockerfile
# services/catalog/Dockerfile (similar pattern for each service)
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir uv

COPY packages/ packages/
COPY services/catalog/ services/catalog/

WORKDIR /build/services/catalog
# In Docker there is no workspace root — switch source from workspace to path
RUN sed -i 's|workspace = true|path = "../../packages/bss-models"|' pyproject.toml \
    && uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python .

FROM python:3.12-slim AS runtime
RUN useradd -m -u 1000 bss && \
    apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder --chown=bss:bss /app/.venv /app/.venv
WORKDIR /app
USER bss
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1
CMD ["python", "-m", "bss_catalog"]
```

**Intended long-term shape (Phase 11+ backlog):** a single shared template Dockerfile that copies the workspace root `pyproject.toml` and `uv.lock`, then uses `uv sync --package ${SERVICE}` from the workspace root. This would eliminate per-service Dockerfile duplication and let uv's workspace resolution work natively.

```dockerfile
# services/_template/Dockerfile — aspirational, not yet implemented
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY services/${SERVICE}/ services/${SERVICE}/
RUN uv sync --package ${SERVICE}

# ... (rest same as per-service pattern)
```

Migration tracked as a Phase 11 backlog item. See DECISIONS.md "Per-service Dockerfile with workspace sed workaround" for the full rationale.

### Footprint budget (motto #6)

Last full re-measurement was v0.6 against the post-v0.5 stack (9 services + 2 portals + OTel SDK + middleware). v0.7–v0.12 added schema-only migrations (catalog versioning, `portal_auth`, `audit.chat_usage`, `audit.chat_transcript`, `crm.case.chat_transcript_hash`) plus per-process in-memory state (chat conversation store, hourly sliding window) — no new container, no measurable RAM bump. The numbers below still represent the practical envelope.

| Component | v0.1 RAM | v0.6 RAM | Notes |
|---|---|---|---|
| 9 × BSS service | ~1.2 GB | ~830 MB | OTel SDK + middleware ~5-10 MB per service |
| 2 × portal (self-serve + csr) | — | ~270 MB | New v0.4 / v0.5; portal-auth + chat-conversation-store add tens of KB of in-memory state per active customer in v0.8/v0.12, immaterial vs the process baseline |
| Postgres (dev config) | ~400 MB | ~400 MB | Unchanged |
| RabbitMQ | ~350 MB | ~350 MB | Unchanged |
| Jaeger (all-in-one image) | — | ~200 MB | New v0.2 |
| **Total (BYOI, services + portals only)** | **~1.5 GB** | **~1.1 GB** | Comfortably under 2 GB |
| **Total (all-in-one, +infra +Jaeger)** | **~2.25 GB** | **~2.05 GB** | Well under the 4 GB motto |

BYOI mode fits on a t3.small; all-in-one fits on a t3.medium. The motto-#6 4 GB ceiling holds with headroom. Re-measure pending v1.0 — real Singpass / Stripe / SM-DP+ adapters will add some footprint (predictably small; SDK clients only).

## Domain boundaries

```
┌─ CRM domain ─────────────────────────────┐
│  Party, Customer, Contact Medium, KYC    │
│  Interaction (audit of touchpoints)      │
│  Case → 1..N Tickets                     │
│  Agent, SLA Policy                       │
│  (hosts Inventory in v0.1)               │
└──────────────────────────────────────────┘

┌─ Inventory domain (inside CRM service) ──┐
│  MSISDN Pool                             │
│  eSIM Profile Pool                       │
└──────────────────────────────────────────┘

┌─ Catalog domain ─────────────────────────┐
│  ProductSpecification                    │
│  ProductOffering (S, M, L)               │
│  ProductOfferingPrice                    │
│  BundleAllowance                         │
│  VAS Offering                            │
│  ServiceSpecification (CFS, RFS)         │
│  ProductToServiceMapping                 │
└──────────────────────────────────────────┘

┌─ Order domain ───────────────────────────┐
│  ProductOrder (COM) ─────decomposes─────┐│
│     │                                    ││
│     └──> ServiceOrder (SOM) ────────────┘│
│              │                            │
│              └──> Service (CFS, RFS)      │
│                      │                    │
│                      └──> ProvisioningTask→ sim
└──────────────────────────────────────────┘

┌─ Subscription domain ────────────────────┐
│  Subscription (FSM)                      │
│  BundleBalance                           │
│  VASPurchase                             │
└──────────────────────────────────────────┘

┌─ Usage → Rating ─────────────────────────┐
│  UsageEvent → RatedDecrement             │
└──────────────────────────────────────────┘

┌─ Audit domain ───────────────────────────┐
│  DomainEvent (outbox + replay substrate) │
└──────────────────────────────────────────┘
```

## Database strategy

### v0.1: single instance, schema-per-domain

```
PostgreSQL 16 + pgvector (one instance)
├── schema crm
├── schema catalog
├── schema inventory         ← MSISDN + eSIM pools
├── schema payment
├── schema order_mgmt        ← COM
├── schema service_inventory ← SOM output
├── schema provisioning      ← simulator
├── schema subscription
├── schema mediation
├── schema billing
├── schema audit             ← domain event log
├── schema integrations      ← v0.14+ external_call + webhook_event + kyc corroboration
├── schema cockpit           ← v0.13 operator cockpit Conversation store
├── schema portal_auth       ← v0.8 self-serve portal sessions
└── schema knowledge         ← v0.20+ doc corpus (FTS + optional pgvector embeddings)
```

> **Postgres image — pgvector required from v0.20.** The bundled
> `docker-compose.infra.yml` ships `pgvector/pgvector:pg16` (drop-in
> same-major replacement for stock `postgres:16-alpine`; data dir
> format identical). BYOI deployments must run
> `CREATE EXTENSION IF NOT EXISTS vector` once on the target Postgres
> before `make migrate`. Migration `0022` activates the extension and
> creates the `knowledge.doc_chunk` table. See
> [`docs/runbooks/knowledge-indexer.md`](docs/runbooks/knowledge-indexer.md).

**Services NEVER read each other's schemas directly.** Cross-service queries go through `bss-clients` HTTP. The shared Postgres instance is a deployment convenience, not a coupling — you can verify this by running `grep -r "schema=" services/` and confirming each service only references its own schema.

In BSS-CLI's actual development, the database is an external Postgres on `tech-vm` (reachable via Tailscale). The shared instance also hosts the `campaignos` schema from a separate production workload — Phase 2's migration and `reset-db` target are scoped to only touch the 11 BSS schemas, never `campaignos` or `public`. This is the real test of schema boundary discipline: co-tenanting a dev BSS with a production schema in the same database, without either one touching the other.

### Why one instance for v0.1

- **Simplicity.** One connection pool per service, one backup target, one monitoring target.
- **Transaction guarantees.** The outbox pattern (audit event + domain write in one TX) is trivial.
- **Resource budget.** Motto #6 — one Postgres is ~400 MB, eleven would be ~4.4 GB and blow the budget.
- **MVNO scale reality.** A single well-tuned Postgres handles millions of subscribers. Splitting before measurement is cargo culting.

### The future split path (post-v0.1)

When a single instance becomes the bottleneck, split by schema:

1. Identify the hot schema (likely `subscription` + `mediation` at first)
2. Stand up a new Postgres instance for that schema
3. Update `BSS_DB_URL` env var for the owning service to point to the new instance
4. Migration is a `pg_dump --schema=foo && pg_restore` plus DNS cutover
5. **Zero service code changes** because each service only knows its own `BSS_DB_URL`

This is the test of whether the v0.1 architecture is honest: can you split without rewriting? Yes, because the schema boundaries enforce the isolation in code today.

## AWS deployment path

### Tier 1 — "Ship on AWS today" (~1 day of work)

Target: development, UAT, proof-of-concept for an MVNO stakeholder.

```
┌──────────────────────────────────────────────────┐
│                  AWS Account                      │
│                                                    │
│   Application Load Balancer                       │
│     ├── /catalog/*          → ECS: catalog         │
│     ├── /customer*/*        → ECS: crm             │
│     ├── /payment*/*         → ECS: payment         │
│     ├── /productOrder*/*    → ECS: com             │
│     ├── /serviceOrder*/*    → ECS: som             │
│     ├── /subscription-api/* → ECS: subscription    │
│     ├── /usage*/*           → ECS: mediation       │
│     ├── /rating-api/*       → ECS: rating          │
│     └── /provisioning-api/* → ECS: provisioning-sim│
│                                                    │
│   ECS Fargate cluster — 9 services, 1 task each    │
│                                                    │
│   RDS PostgreSQL (db.t4g.medium)                   │
│   Amazon MQ for RabbitMQ (mq.m5.large)             │
│   ECR for container images                          │
│   CloudWatch Logs (structured JSON)                │
│   Secrets Manager (DB credentials, future auth)    │
│                                                    │
│   Cost estimate: ~$400/month                       │
└──────────────────────────────────────────────────┘
```

**Why this maps cleanly from v0.1:** every service is already a container with the right healthcheck, non-root user, structured logging, and env-driven config. Your Campaign OS ECS Fargate experience transfers directly — it's the same deployment model with different container images.

### Tier 2 — "Small MVNO production" (~1 week on top of v0.1)

Target: launching for a real customer base of 10,000-50,000 subscribers.

Additions on top of Tier 1:
- RDS PostgreSQL **Multi-AZ** for HA
- Amazon MQ **active/standby**
- ECS Fargate **min 2 tasks per service** for rolling deploys and HA
- TLS termination at ALB via ACM certificate
- Route53 for DNS and health checks
- CloudWatch Alarms on key metrics (p99 latency, error rate, queue depth, bundle exhaustion rate)
- Secrets Manager rotation enabled
- WAF basic ruleset on ALB
- **Authentication (Phase 12)** is required before this tier — no TLS-terminated internet exposure without auth

Cost estimate: **~$800-1,200/month** at 10k subscribers.

### Tier 3 — "Scaled MVNO" (post-v0.2, ~1 month of work)

Target: 100k+ subscribers, multi-region, strict SLA.

- **EKS** instead of ECS (better for mixed stateful/stateless workloads at scale)
- **RDS Aurora PostgreSQL** with read replicas, or Aurora per service
- **MSK (Managed Kafka)** replacing RabbitMQ for >20k events/sec
- **ElastiCache Redis** for hot-path caching and rate limiting
- **CloudFront** for eSIM activation asset delivery
- **Shield Advanced**, stricter WAF rules
- **CloudHSM** for real Ki storage (compliance requirement at scale)
- **Multi-region** with cross-region replication for DR

Cost estimate: **~$4,000-8,000/month**.

### Deployability matrix

(Tier-3 view — every capability needed to ship the platform onto a scaled MVNO. The v0.11 / v0.12 / v1.0 matrix above is the v0.12 readiness pass for the chat surface specifically.)

| Capability | v0.12 status | Notes |
|---|---|---|
| Docker Compose bring-up | ✅ | BYOI is the default shape; all-in-one compose exists for quickstart |
| ECS Fargate | ✅ | Each service is a task definition |
| EKS | ✅ | Same containers, need K8s manifests (not in v0.12) |
| Horizontal scale stateless services | ✅ | Catalog, rating, provisioning-sim scale trivially |
| Horizontal scale stateful services | ⚠️ | Requires consistent-hash routing by customer_id (Phase 13) |
| Multi-AZ database | ✅ | Postgres connection URL is env-driven |
| Zero-downtime deploys | ⚠️ | Needs graceful SIGTERM handler (wired in Phase 3 reference slice) |
| TLS termination | ➖ | Expected at ALB / ingress layer, not per-service |
| Auth between services | ⚠️ | Shared API token (v0.3) via `BSSApiTokenMiddleware` + `TokenAuthProvider`; v0.9 splits the perimeter into named tokens (`TokenMap`). Per-principal OAuth2 + JWT is Phase 12. `auth_context.py` seam unchanged — Phase 12 fills the principal from JWT claims. |
| Per-portal named tokens | ✅ | v0.9 splits the perimeter. Self-serve portal carries `BSS_PORTAL_SELF_SERVE_API_TOKEN` → `service_identity="portal_self_serve"`; orchestrator + CSR keep `BSS_API_TOKEN` (default identity). `service_identity` flows into `audit.domain_event`, structlog, OTel spans. Rotation is per-token, restart-based. |
| Operator-facing portal auth | ✅ (by design) | v0.13 cockpit on port 9002 has no inbound auth — single-operator-by-design behind a secure perimeter. `actor` from `.bss-cli/settings.toml`. Phase-12 staff-auth retired (DECISIONS 2026-05-01). Trusted-network deploy only. |
| Customer-facing portal auth | ✅ | Self-serve portal on 9001 ships with v0.8 email + magic-link / OTP behind `PortalSessionMiddleware`; v0.10 adds step-up auth gating every sensitive write (`SENSITIVE_ACTION_LABELS`). Public-route allowlist (`/welcome`, `/plans`, `/auth/*`, `/terms`, `/privacy`) explicit. `/signup/*` is gated on `requires_verified_email`. Per-principal OAuth Phase 12. |
| Customer chat surface scoping | ✅ | v0.12 — `customer_self_serve` tool profile (16 curated `*.mine` wrappers + public catalog reads) + output ownership trip-wire + per-customer rate + monthly cost caps (`audit.chat_usage`, fail-closed). Five non-negotiable escalation categories via `case.open_for_me` with SHA-256-hashed transcript. 14-day soak: zero ownership trips, zero cross-customer leaks, drift 0%. |
| Rate limiting per principal | ⚠️ | v0.12 caps the chat surface per customer (rate + monthly cost). General per-principal rate limiting on every BSS endpoint is Phase 12. |
| Distributed tracing | ✅ | OpenTelemetry to Jaeger (v0.2). W3C traceparent through HTTP / MQ / SQL. `bss trace` renders ASCII swimlanes. |
| Metrics export | ❌ | Counters/histograms go to structlog only. OTel metrics export decision pending — see ROADMAP.md "Near-term". |
| uv workspace builds in CI | ⚠️ | Per-service Dockerfile with `sed` rewrite workaround (Phase 4 expedient). |
| Schema boundary enforcement | ✅ | Each service only references its own schema; verified by grep. Co-tenant with Campaign OS in dev proves this. |
| KYC / payment / eSIM provisioning | ⚠️ | All three mocked in v0.12 (`KYC-PREBAKED-001` attestation, sandbox card tokenizer, provisioning-sim). v1.0 swaps Singpass + Stripe + real SM-DP+ behind the existing seams; nothing else in v0.7–v0.12 is renegotiated. |

## Observability (v0.2)

- **OpenTelemetry SDK** in every service via `bss-telemetry`. Auto-instrumentors hook FastAPI (server spans), HTTPX (outbound), AsyncPG (SQL via SQLAlchemy), and aio-pika (MQ publish/consume). W3C `traceparent` propagates through HTTP, MQ messages, and SQL spans automatically.
- **Three manual span sites** add business semantics that auto can't infer: `com.order.complete_to_subscription`, `som.decompose`, `subscription.purchase_vas`. Verified via grep guard.
- **Jaeger all-in-one** as the trace backend. OTLP/HTTP ingress on `:4318`, UI on `:16686`. Memory storage by default; swap to badger for persistence (see `docs/runbooks/jaeger-byoi.md`). Two deploy paths:
  - **Bundled:** `docker-compose.infra.yml` includes the `jaeger` service alongside postgres + rabbitmq.
  - **BYOI:** install Jaeger once on the same host that already runs Postgres/RabbitMQ (typically tech-vm). Same image, same ports.
- **`bss trace <id>`** queries Jaeger's HTTP API and renders an ASCII swimlane (services as columns, parent-child indented, manual spans starred). Supplements `for-order` / `for-subscription` / `for-ask` resolvers that join through `audit.domain_event.trace_id`.
- **`audit.domain_event.trace_id`** populated on every write by the per-service publishers via `bss_telemetry.current_trace_id()`. Enables post-hoc lookups from a business ID to the full distributed trace.
- **`/health` excluded** from instrumentation (OTel `excluded_urls`). Without this the Jaeger UI is 99% docker-healthcheck noise.
- **structlog** continues to JSON-log; `trace_id` correlation in log lines is present from v0.1 forward-compat work.
- **Business analytics** is out of scope in-tree: `audit.domain_event` is the substrate, and any external BI consumer plugs in BYOI against the shared Postgres — separate from OTel, different consumer of the same audit log.

## What's NOT in the architecture

- **No API gateway.** CLI talks directly to services. Simpler, lower latency, easier to debug. ALB is the gateway in AWS deployments.
- **No service mesh.** Docker network / VPC routing is sufficient below 100k RPS.
- **No Kafka.** RabbitMQ is lighter, simpler, and topic exchanges cover v0.1 needs. Migration path to MSK is documented for Tier 3.
- **No Redis.** Postgres is fast enough for v0.1 workload.
- **No staff-side authentication.** Retired in v0.13 (DECISIONS 2026-05-01). The cockpit is single-operator-by-design behind a secure perimeter; `actor` from `.bss-cli/settings.toml`. Customer-side auth (v0.8 portal session, v0.10 step-up) is unchanged. The `auth_context.py` seam in every service stays as it was — it just stops carrying a planned future shape.
- **No multi-tenancy at runtime.** `tenant_id` columns exist but default to `'DEFAULT'`. Activating true tenancy is a v0.3 concern.
- **No eKYC implementation.** Channel-layer concern. BSS-CLI receives signed attestations via `customer.attest_kyc` and enforces policies.
- **No physical SIM logistics.** eSIM-only in v0.1.
- **No strict event ordering.** Consumers must handle concurrent events causally via policy checks, not assume arrival order. See "Event ordering guarantees" section above.
- **No Online Charging System (OCS).** BSS-CLI does not implement Diameter Gy/Ro, PCEF quota grants, quota reservation, or `Final-Unit-Indication` signalling to the packet core. OCS is abstracted outside the solution — a real deployment would have an external OCS on the network side making live authorize/deny decisions against the PCEF/GGSN. Our Mediation service is **TMF635 online mediation**: single-event ingest with synchronous block-at-edge policy, driving balance decrement via events. It collapses the customer-facing accounting surface of an OCS (quota depletion → block) into a TMF-shaped REST API, but it does not sit on the data plane.
- **No batch mediation.** No CDR file ingest, no hourly/daily aggregation jobs, no rerating windows, no deduplication/correlation pipelines. Motto #1 (bundled-prepaid only) removes the reason batch mediation exists — there are no per-unit charges to roll up into an invoice. If post-paid is ever introduced (v0.3+), a batch-rating plane would need to be added alongside the current online path; it is not a modification of it.
