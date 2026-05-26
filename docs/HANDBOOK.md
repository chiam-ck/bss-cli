---
title: BSS-CLI Operator Handbook
version: 0.19.1
audience: operator, deployer, developer, knowledge-base
updated: 2026-05-04
tags: [bss-cli, handbook, runbook, reference]
---

# BSS-CLI Operator Handbook

> Single canonical reference for **operating, deploying, and troubleshooting BSS-CLI**. Optimized for Obsidian (back-links, table-of-contents, anchor-jump). Designed to be ingestible by the REPL/Cockpit when a tool answer is insufficient.
>
> **Repo doctrine** lives in [`CLAUDE.md`](../CLAUDE.md). **Architecture** lives in [`ARCHITECTURE.md`](../ARCHITECTURE.md). **Decisions log** lives in [`DECISIONS.md`](../DECISIONS.md). This handbook synthesizes them into one navigable document; when a fact differs, the source-of-truth files win.

---

## Table of Contents

- [Part 1 — What this is](#part-1--what-this-is)
- [Part 2 — Quick start (5-minute path)](#part-2--quick-start-5-minute-path)
- [Part 3 — Setup](#part-3--setup)
  - [3.1 Deployment shapes — bundled vs BYOI](#31-deployment-shapes--bundled-vs-byoi)
  - [3.2 First-run path (full)](#32-first-run-path-full)
  - [3.3 Environment variables — full catalogue](#33-environment-variables--full-catalogue)
  - [3.4 `make` targets](#34-make-targets)
  - [3.5 Cockpit operator preferences (`settings.toml` + `OPERATOR.md`)](#35-cockpit-operator-preferences-settingstoml--operatormd)
- [Part 4 — External providers](#part-4--external-providers)
  - [4.1 The adapter pattern](#41-the-adapter-pattern)
  - [4.2 Resend (transactional email, v0.14)](#42-resend-transactional-email-v014)
  - [4.3 Didit (KYC verification, v0.15)](#43-didit-kyc-verification-v015)
  - [4.4 Stripe (payment tokenization + charging, v0.16)](#44-stripe-payment-tokenization--charging-v016)
- [Part 5 — Operator guide](#part-5--operator-guide)
  - [5.1 The REPL (`bss`) — canonical cockpit](#51-the-repl-bss--canonical-cockpit)
  - [5.2 Browser cockpit (`localhost:9002`)](#52-browser-cockpit-localhost9002)
  - [5.3 Direct Typer subcommands (every leaf)](#53-direct-typer-subcommands-every-leaf)
  - [5.4 Slash commands](#54-slash-commands)
  - [5.5 Cockpit LLM tools (`operator_cockpit` profile)](#55-cockpit-llm-tools-operator_cockpit-profile)
  - [5.6 Destructive-tool gating (`/confirm`)](#56-destructive-tool-gating-confirm)
- [Part 6 — Customer guide](#part-6--customer-guide)
  - [6.1 Self-serve portal (`localhost:9001`)](#61-self-serve-portal-localhost9001)
  - [6.2 Customer chat (the only orchestrator-mediated route)](#62-customer-chat-the-only-orchestrator-mediated-route)
  - [6.3 Step-up auth labels](#63-step-up-auth-labels)
- [Part 7 — Domain features](#part-7--domain-features)
  - [7.1 Catalog — offerings, VAS, allowances, roaming](#71-catalog--offerings-vas-allowances-roaming)
  - [7.2 Customer signup with KYC](#72-customer-signup-with-kyc)
  - [7.3 Payment methods + charging](#73-payment-methods--charging)
  - [7.4 Subscription lifecycle](#74-subscription-lifecycle)
  - [7.5 Automatic renewal worker (v0.18)](#75-automatic-renewal-worker-v018)
  - [7.6 Roaming (v0.17)](#76-roaming-v017)
  - [7.7 Number portability — port-in / port-out (v0.17)](#77-number-portability--port-in--port-out-v017)
  - [7.8 VAS top-ups](#78-vas-top-ups)
  - [7.9 Plan changes (v0.7+)](#79-plan-changes-v07)
  - [7.10 Escalation cases](#710-escalation-cases)
  - [7.11 Chat surface (v0.11–v0.12)](#711-chat-surface-v011v012)
  - [7.12 Operator cockpit (v0.13)](#712-operator-cockpit-v013)
  - [7.13 Tracing & audit](#713-tracing--audit)
  - [7.14 Cockpit conversation rendering (v0.19)](#714-cockpit-conversation-rendering-v019)
  - [7.15 Knowledge tool (v0.20)](#715-knowledge-tool-v020)
- [Part 8 — Day-2 operations (runbooks)](#part-8--day-2-operations-runbooks)
  - [8.1 Catalog — add an offering (with roaming)](#81-catalog--add-an-offering-with-roaming)
  - [8.2 Catalog — run a promo](#82-catalog--run-a-promo)
  - [8.3 Migrate customers to a new price](#83-migrate-customers-to-a-new-price)
  - [8.4 Rotate API tokens](#84-rotate-api-tokens)
  - [8.5 Stripe cutover (mock → production)](#85-stripe-cutover-mock--production)
  - [8.6 Three-provider sandbox soak (release gate)](#86-three-provider-sandbox-soak-release-gate)
  - [8.7 MNP — port-in / port-out](#87-mnp--port-in--port-out)
  - [8.8 Chat — cap tripped](#88-chat--cap-tripped)
  - [8.9 Chat — ownership-trip (P0)](#89-chat--ownership-trip-p0)
  - [8.10 Chat — triage an escalated case](#810-chat--triage-an-escalated-case)
  - [8.11 Chat — transcript retention](#811-chat--transcript-retention)
  - [8.12 Post-login self-serve diagnostics](#812-post-login-self-serve-diagnostics)
  - [8.13 Portal auth ops (pepper, identities, brute force)](#813-portal-auth-ops-pepper-identities-brute-force)
  - [8.14 Cockpit ops (sessions, persona, settings, /confirm forensics)](#814-cockpit-ops-sessions-persona-settings-confirm-forensics)
  - [8.15 Jaeger BYOI (set up tracing on a BYO host)](#815-jaeger-byoi-set-up-tracing-on-a-byo-host)
  - [8.16 Snapshot regeneration (CLI golden files)](#816-snapshot-regeneration-cli-golden-files)
  - [8.17 Payment idempotency forensics](#817-payment-idempotency-forensics)
  - [8.18 Adding a tool to `customer_self_serve` (security review)](#818-adding-a-tool-to-customer_self_serve-security-review)
  - [8.19 Knowledge indexer reindex / Postgres pgvector prereq](#819-knowledge-indexer-reindex--postgres-pgvector-prereq)
- [Part 9 — Anti-patterns & doctrine ("Don'ts")](#part-9--anti-patterns--doctrine-donts)
- [Part 10 — Reference appendix](#part-10--reference-appendix)
  - [10.1 Glossary](#101-glossary)
  - [10.2 Schemas at a glance](#102-schemas-at-a-glance)
  - [10.3 Domain events (cheat-sheet)](#103-domain-events-cheat-sheet)
  - [10.4 Tool profiles (cockpit vs chat)](#104-tool-profiles-cockpit-vs-chat)
  - [10.5 Portal routes (every one)](#105-portal-routes-every-one)
  - [10.6 IDs (prefixed strings)](#106-ids-prefixed-strings)
  - [10.7 Doc map (where to look for what)](#107-doc-map-where-to-look-for-what)

---

## Part 1 — What this is

**BSS-CLI** is a complete, lightweight, SID-aligned, TMF-compliant Business Support System designed to run entirely from a terminal. It is **LLM-native**: every operation is exposed as a tool the LLM can call, and the primary UI is the CLI plus ASCII-rendered visualizations. Analytics is out of scope in-tree: `audit.domain_event` is the substrate, plug in any external BI consumer BYOI.

**Three audiences:**
1. **Engineers learning telco BSS/OSS** — read [`ARCHITECTURE.md`](../ARCHITECTURE.md) and the phase docs in `phases/`.
2. **Small MVNO operators** — this handbook is for you. You run it; you debug it; you extend it.
3. **Agentic experimenters** — the orchestrator + tool-profile model is the substrate.

### The seven motto principles (NEVER violate)

1. **Bundled-prepaid only.** No proration. No dunning. No collections. No credit-risk modeling.
2. **Card-on-file is mandatory.** Failed charge equals no service. No grace period. No retry ladder.
3. **Block-on-exhaust.** Service stops the instant a bundle hits zero. The only paths back: bundle renewal (auto, on period boundary, COF) or VAS top-up (explicit, COF).
4. **CLI-first, LLM-native.** Every capability is a tool. Terminal is primary. ASCII is the visualization language. No graphical analytics dashboard ships in-tree (`audit.domain_event` is the BYOI substrate).
5. **TMF-compliant where it counts.** Real TMF Open API surfaces. Not naming theater.
6. **Lightweight is measurable.** Full stack <4GB RAM, cold start <30s, p99 internal API latency <50ms.
7. **Write through policy, read freely.** Every write goes through invariant-enforcing policies. There is no raw CRUD.

### Scope boundaries — what BSS-CLI is NOT

- **eKYC** — receives signed attestations from the channel layer and records them. Document capture / liveness / biometric matching are channel-layer concerns. v0.15+ ships the **Didit** integration for the BSS-managed channel reference impl, but the redacting boundary is at the BSS edge.
- **Customer-facing UI beyond a reference self-serve portal** — mobile apps, retail POS, USSD: channel layer.
- **Network elements** — HLR/HSS, PCRF, OCS, SM-DP+ are simulated in v0.x.
- **eSIM redownload re-arm flow** — real GSMA SGP.22 redownload requires SM-DP+ to release / re-arm; v0.10's `/esim/<id>` is a read-only re-display of the activation code minted at signup.
- **Physical SIM** — eSIM-only.
- **CDR collection from RAN** — Mediation accepts already-parsed CDRs via API.
- **Online Charging System (OCS)** — Diameter Gy/Ro is abstracted outside BSS-CLI.
- **Tax calculation** — SGD inclusive pricing in v0.x. Vertex/Avalara is post-v0.x.
- **Regulatory reporting** — extraction jobs against `audit.domain_event`, not built-in.

### Two trust principals (not five personas)

The system has **two** trust principals:
- **Operator** — REPL + browser cockpit, single-operator-by-design behind a secure perimeter, `actor` from `settings.toml` (descriptive only).
- **Customer** — self-serve portal + chat, per-principal email-based session, server-side cookies, step-up auth on sensitive writes.

Phase-12 OAuth/RBAC for staff is **retired** (DECISIONS 2026-05-01), not deferred. If a future ops setup needs multi-operator separation, the path is multi-tenant carve-out (one cockpit container per namespace), **not** a login wall.

---

## Part 2 — Quick start (5-minute path)

> **Prerequisites:** Docker + Docker Compose, Python 3.12, [`uv`](https://docs.astral.sh/uv/), and an OpenRouter API key (free at openrouter.ai). Linux / macOS. ~4 GB RAM headroom.

```bash
git clone <repo-url>
cd bss-cli

# 1. Seed .env from example.
cp .env.example .env

# 2. Replace the two "changeme" sentinels — they're rejected at startup.
sed -i "s/^BSS_API_TOKEN=changeme$/BSS_API_TOKEN=$(openssl rand -hex 32)/" .env
sed -i "s/^BSS_PORTAL_TOKEN_PEPPER=changeme$/BSS_PORTAL_TOKEN_PEPPER=$(openssl rand -hex 32)/" .env

# 3. Set BSS_LLM_API_KEY in .env to your OpenRouter key (sk-or-v1-...).

# 4. Bring everything up (bundled = local Postgres + RabbitMQ + Jaeger).
make up-all

# 5. Apply migrations + seed reference data (3 plans, 4 VAS, 1000 MSISDNs, 1000 eSIM profiles).
make migrate
make seed

# 6. Open the cockpit REPL.
bss
```

Inside the REPL, try:

```
list customers
list plans
show port requests
```

Or natural language: `tell me about plan_m`. The orchestrator dispatches the right tool, the renderer prints an ASCII card, the conversation persists.

The portals are at:
- **`http://localhost:9001`** — self-serve customer portal (sign up, top up, plan change)
- **`http://localhost:9002`** — operator browser cockpit (same conversations as the REPL)
- **`http://localhost:16686`** — Jaeger trace UI
- **`http://localhost:15672`** — RabbitMQ UI (`guest/guest`)

To wire up real providers (Resend / Didit / Stripe), run `bss onboard` (interactive) — see [Part 4](#part-4--external-providers).

---

## Part 3 — Setup

### 3.1 Deployment shapes — bundled vs BYOI

BSS-CLI supports two deployment shapes from the same compose files. The switch is **purely connection-string driven** — there is no `BSS_INFRA_MODE` env var.

#### Bundled (all-in-one) — recommended for dev / demo / small MVNO

```bash
docker compose -f docker-compose.yml -f docker-compose.infra.yml up -d
# or:
make up-all
```

Brings up **9 BSS service containers + 2 portal containers + 3 infrastructure containers**:

| Container | Image | Ports | Purpose |
|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | `5432:5432` | DB (`bss/bss/bss`), volume `postgres_data` |
| `rabbitmq` | `rabbitmq:3.13-management-alpine` | `5672:5672`, `15672:15672` | MQ (`guest/guest`), volume `rabbitmq_data` |
| `jaeger` | `jaegertracing/all-in-one:1.65.0` | `4317`, `4318`, `16686` | OTLP collector + UI, in-memory storage (lost on restart) |

#### BYOI (bring-your-own-infra) — for shared infrastructure / production

```bash
docker compose up -d
# or:
make up
```

Runs **only** the 9 + 2 service/portal containers. Operator points `.env` at their existing Postgres, RabbitMQ, and Jaeger:

```bash
BSS_DB_URL=postgresql+asyncpg://bss:secret@db.host:5432/bss
BSS_MQ_URL=amqp://user:pw@mq.host:5672/
BSS_OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger.host:4318
```

#### Service container map (constant across both shapes)

| Service | Host port | Purpose |
|---|---|---|
| `catalog` | 8001 | Product catalogue (TMF620), bundle allowances |
| `crm` | 8002 | Customer + cases + tickets + port-requests + KYC (TMF629/TMF621) |
| `payment` | 8003 | Tokenization + charging + refunds (Stripe / mock) |
| `com` | 8004 | Customer Order Management (TMF622) |
| `som` | 8005 | Service Order Management (TMF641 + TMF638) |
| `subscription` | 8006 | Subscription FSM, balances, renewal worker, plan-change |
| `mediation` | 8007 | TMF635 online mediation, usage events |
| `rating` | 8008 | Rating engine, roaming routing |
| _(8009 reserved)_ | — | Billing service deferred (DECISIONS 2026-04-13) |
| `provisioning-sim` | 8010 | HLR/PCRF/OCS/SM-DP+ simulator |
| `portal-self-serve` | 9001 | Customer self-serve + chat |
| `portal-csr` | 9002 | Operator browser cockpit (historical name; NOT a separate CSR app) |

Bind mounts of note:
- `./.dev-mailbox → /var/bss-mailbox` on `portal-self-serve` — `LoggingEmailAdapter` writes OTPs/links here.
- `./.bss-cli → /cockpit-state` on `portal-csr` — `OPERATOR.md`, `settings.toml`, REPL history.

### 3.2 First-run path (full)

```bash
# 1. Clone + .env
git clone <repo-url>
cd bss-cli
cp .env.example .env

# 2. Generate real secrets (sentinels are rejected at startup).
sed -i "s/^BSS_API_TOKEN=changeme$/BSS_API_TOKEN=$(openssl rand -hex 32)/" .env
sed -i "s/^BSS_PORTAL_TOKEN_PEPPER=changeme$/BSS_PORTAL_TOKEN_PEPPER=$(openssl rand -hex 32)/" .env

# 3. Set BSS_LLM_API_KEY to your OpenRouter key.
# Edit .env directly. Default model: deepseek/deepseek-v4-pro (v1.5.1+; previously gemma-4-26b-a4b-it).

# 4a. Bundled mode:
make up-all

# 4b. OR BYOI mode — first edit BSS_DB_URL / BSS_MQ_URL / BSS_OTEL_EXPORTER_OTLP_ENDPOINT, then:
make up

# 5. Apply Alembic migrations (head currently 0021 on v0.19.1).
make migrate

# 6. Seed reference data: 3 plans + 4 VAS (incl. roaming) + 1000 MSISDNs + 1000 eSIM profiles.
make seed

# 7. (Optional) Wire up real providers.
bss onboard                  # walks email + kyc + payment in order
# OR per domain:
bss onboard --domain email
bss onboard --domain kyc
bss onboard --domain payment
# After onboard, restart services so lifespans pick up the new env:
docker compose down && docker compose up -d

# 8. Open the cockpit REPL.
bss

# 9. Self-test the install.
make scenarios               # 17 hero scenarios, ~95s; auto-flips providers to mock
make doctrine-check          # 14 grep guards
```

**Files created or modified along the way:**
- `.env` — operator config (created at step 1, mutated by `bss onboard`)
- `.env.backup-YYYY-MM-DD-HHMMSS` — `bss onboard` keeps the most recent 5
- `.dev-mailbox/` — created `0777` by `make up`'s `dev-mailbox-dir` target before bind-mount
- `.bss-cli/OPERATOR.md` + `.bss-cli/settings.toml` — autobootstrapped from `.template` files on first cockpit run
- `.bss-cli/repl_history` — REPL command history (prompt_toolkit)

> [!warning] **Don't commit `.env`.** It contains secrets. The `.gitignore` covers `.env`, `.env.backup-*`, but watch for `.env.bak*` shapes left behind if `make scenarios` crashes mid-run.

### 3.3 Environment variables — full catalogue

Source of truth: [`.env.example`](../.env.example). Sentinel values (`changeme`, `sk-or-v1-replace-me`) are rejected at startup.

#### Infrastructure

| Var | What | Default | Required | Notes |
|---|---|---|---|---|
| `BSS_DB_URL` | Async Postgres URL (asyncpg dialect) | `postgresql+asyncpg://bss:bss@postgres:5432/bss` | yes | BYOI: change host |
| `BSS_MQ_URL` | RabbitMQ AMQP URL | `amqp://guest:guest@rabbitmq:5672/` | yes | BYOI: change host |

#### Application policy

| Var | What | Default | Notes |
|---|---|---|---|
| `BSS_ENV` | `development` / `staging` / `production` | `development` | **Doctrine flag** — many startup guards key on `production` (sk_test_* refusal, prebaked-KYC refusal, PCI template scan) |
| `BSS_LOG_LEVEL` | structlog level | `INFO` | |
| `BSS_TENANT_DEFAULT` | Default tenant_id stamped on writes | `DEFAULT` | |
| `BSS_ALLOW_ADMIN_RESET` | Gates `admin.reset_operational_data` | `false` | NEVER true in production |
| `BSS_ALLOW_DESTRUCTIVE` | Gates a few CLI commands (`bss payment remove-method`) | `false` | CLI prompts when needed |
| `BSS_REQUIRE_KYC` | Phase 4+ KYC enforcement on order placement | `false` | |
| `BSS_ENABLE_TEST_ENDPOINTS` | Exposes `/dev/*` tokenizer endpoint | `false` | For `bss payment add-card` in mock mode |
| `BSS_CLOCK_MODE` | `system` (real wallclock) vs admin-controlled | `system` | Scenario harnesses freeze it |

#### BSS perimeter API tokens (v0.3 default, v0.9 named)

Loaded once at startup into a `TokenMap` (HMAC-SHA-256 hashed). Identity derived from the env-var name. ≥32 chars enforced; sentinel `"changeme"` rejected; sharing one across surfaces is forbidden by code.

| Var | Identity | Required | Notes |
|---|---|---|---|
| `BSS_API_TOKEN` | `default` | yes | Orchestrator + REPL + scenarios. Generate with `openssl rand -hex 32` |
| `BSS_PORTAL_SELF_SERVE_API_TOKEN` | `portal_self_serve` | no (falls back to `BSS_API_TOKEN`) | Self-serve portal outbound identity |
| `BSS_OPERATOR_COCKPIT_API_TOKEN` | `operator_cockpit` | no (falls back) | Cockpit outbound identity |
| `BSS_<NAME>_API_TOKEN` | `<name>` (lowercased) | no | Pattern for new partners (e.g. `BSS_PARTNER_ACME_API_TOKEN` → identity `partner_acme`) |

#### LLM (orchestrator + chat + cockpit)

| Var | What | Default | Required |
|---|---|---|---|
| `BSS_LLM_BASE_URL` | OpenRouter URL | `https://openrouter.ai/api/v1` | yes |
| `BSS_LLM_MODEL` | Model id (overridable per-cockpit by `settings.toml [llm].model`) | `deepseek/deepseek-v4-pro` | yes |
| `BSS_LLM_API_KEY` | OpenRouter key | sentinel `sk-or-v1-replace-me` (rejected) | yes |
| `BSS_LLM_HTTP_REFERER` | OpenRouter routing hint | repo URL | no |
| `BSS_LLM_APP_NAME` | OpenRouter app tag | `bss-cli` | no |
| `BSS_REPL_LLM_AUTONOMY` | Cockpit autonomy mode (v1.5+): `granular` or `batched`. See 8.16. | `granular` | no |

#### Chat caps (v0.12, customer chat surface only)

| Var | What | Default |
|---|---|---|
| `BSS_CHAT_RATE_PER_CUSTOMER_PER_HOUR` | Sliding-window per-customer rate cap | 20 |
| `BSS_CHAT_COST_CAP_PER_CUSTOMER_PER_MONTH_CENTS` | Monthly cost ceiling, persisted in `audit.chat_usage` | 200 |
| `BSS_CHAT_RATE_PER_IP_PER_HOUR` | Sliding-window per-IP rate cap | 60 |

#### OpenTelemetry (v0.2)

| Var | What | Default |
|---|---|---|
| `BSS_OTEL_ENABLED` | Master switch | `true` |
| `BSS_OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector URL | `http://jaeger:4318` |
| `BSS_OTEL_EXPORTER_OTLP_PROTOCOL` | OTLP transport | `http/protobuf` |
| `BSS_OTEL_SERVICE_NAME_PREFIX` | Span service-name prefix | `bss` |
| `BSS_OTEL_SAMPLING_RATIO` | 0..1 | `1.0` |

#### Self-serve portal auth (v0.8)

| Var | What | Default | Required |
|---|---|---|---|
| `BSS_PORTAL_TOKEN_PEPPER` | HMAC pepper for OTP / magic-link / step-up tokens. ≥32 chars | `changeme` (rejected) | yes |
| `BSS_PORTAL_DEV_INSECURE_COOKIE` | Drop `Secure` cookie attribute (HTTP-only dev) | `1` (compose default) | dev only |
| `BSS_PORTAL_PUBLIC_URL` | Base for outbound magic-link URLs + Didit return URLs | `http://localhost:9001` | yes (prod) |
| `BSS_PORTAL_LOGIN_PER_EMAIL_MAX` / `_WINDOW_S` | Rate-limit | `3` / `900` | |
| `BSS_PORTAL_LOGIN_PER_IP_MAX` / `_WINDOW_S` | Rate-limit | `10` / `3600` | |
| `BSS_PORTAL_VERIFY_PER_EMAIL_MAX` / `_WINDOW_S` | Rate-limit | `10` / `900` | |
| `BSS_PORTAL_STEPUP_PER_SESSION_MAX` / `_WINDOW_S` | Rate-limit | `5` / `900` | |

#### Operator branding (v0.19, surfaced in customer chat)

| Var | What | Default |
|---|---|---|
| `BSS_OPERATOR_SUPPORT_EMAIL` | Address the chat bot directs unanswered queries to | `support@bss-cli.local` |
| `BSS_OPERATOR_NAME` | Brand the chat bot greets with | `BSS-CLI Mobile` |

#### Email provider (v0.14)

| Var | What | Default | Notes |
|---|---|---|---|
| `BSS_PORTAL_EMAIL_PROVIDER` | `logging` / `noop` / `resend` | `logging` | `BSS_PORTAL_EMAIL_ADAPTER` is the v0.8 alias (deprecated) |
| `BSS_PORTAL_DEV_MAILBOX_PATH` | File `LoggingEmailAdapter` writes OTPs/links to | `/tmp/bss-portal-mailbox.log` | Compose pins `/var/bss-mailbox/portal-mailbox.log` |
| `BSS_PORTAL_EMAIL_RESEND_API_KEY` | Resend secret (`re_*`) | unset | Required when provider=`resend` |
| `BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET` | Resend webhook (`whsec_*`) | unset | For `/webhooks/resend` |
| `BSS_PORTAL_EMAIL_FROM` | Sender envelope, e.g. `BSS-CLI <noreply@…>` | unset | Required when provider=`resend` |

#### KYC provider (v0.15)

| Var | What | Default |
|---|---|---|
| `BSS_PORTAL_KYC_PROVIDER` | `prebaked` / `didit` | `prebaked` |
| `BSS_PORTAL_KYC_DIDIT_API_KEY` | Didit secret | unset |
| `BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID` | Didit workflow UUID (raw, no `wf_` prefix) | unset |
| `BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET` | Didit HMAC secret — **non-negotiable trust anchor** | unset |
| `BSS_KYC_ALLOW_PREBAKED` | Accept `provider="prebaked"` attestations in production | `false` (true outside prod) | Refused at startup if `BSS_ENV=production` and not `true` explicitly |
| `BSS_KYC_ALLOW_DOC_REUSE` | Bypass document-hash uniqueness (Didit sandbox returns stable test NRIC) | `false` | Refused at startup if `BSS_ENV=production` |

#### Payment provider (v0.16)

| Var | What | Default |
|---|---|---|
| `BSS_PAYMENT_PROVIDER` | `mock` / `stripe` | `mock` |
| `BSS_PAYMENT_STRIPE_API_KEY` | `sk_test_*` / `sk_live_*` | unset |
| `BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY` | `pk_test_*` / `pk_live_*` (must match secret-key mode) | unset |
| `BSS_PAYMENT_STRIPE_WEBHOOK_SECRET` | `whsec_*` — **non-negotiable** | unset |
| `BSS_PAYMENT_ALLOW_TEST_CARD_REUSE` | Sandbox-only `pm_card_visa` reuse | `false` | Refused if paired with `sk_live_*` |
| `BSS_PAYMENT_WEBHOOK_PUBLIC_URL` | Public URL for Tier-5 live-sandbox soak only | unset (Tier 5 skips) |
| `BSS_NIGHTLY_SANDBOX` | Soak Tier-5 toggle | unset |

#### eSIM provider (v0.15+)

| Var | What | Default |
|---|---|---|
| `BSS_ESIM_PROVIDER` | `sim` (only impl); `onbglobal` / `esim_access` are stubs that raise on first use | `sim` |

#### Inventory + renewal (v0.17 / v0.18)

| Var | What | Default |
|---|---|---|
| `BSS_INVENTORY_MSISDN_POOL_LOW_THRESHOLD` | Available-count threshold that emits `inventory.msisdn.pool_low` | 50 |
| `BSS_RENEWAL_TICK_SECONDS` | Subscription-service in-process renewal worker tick (0 disables) | 60 |
| `BSS_RENEWAL_REMINDER_LOOKAHEAD_SECONDS` | Window for upcoming-renewal reminder email (0 disables; same `EmailAdapter`) | 86400 |

#### Cockpit container only

| Var | What | Default |
|---|---|---|
| `BSS_COCKPIT_DIR` | Override for `.bss-cli/` directory inside the container | repo-relative (compose maps `/cockpit-state`) |

#### Knowledge tool (v0.20+, operator_cockpit only)

| Var | What | Default |
|---|---|---|
| `BSS_KNOWLEDGE_ENABLED` | Master switch for the cockpit's `knowledge.search` / `knowledge.get` tools. When `false`, tools are not registered and the citation guard relaxes | `true` |
| `BSS_KNOWLEDGE_BACKEND` | `fts` (Postgres FTS only; default; zero deps beyond pgvector being installable) or `hybrid` (pgvector cosine + FTS re-rank) | `fts` |
| `BSS_KNOWLEDGE_EMBEDDER` | When `BACKEND=hybrid`: `openrouter` (re-uses `BSS_LLM_API_KEY`) or `local` (sentence-transformers, offline) | `openrouter` |

#### Test/scenario-only

- `BSS_ONBOARD_ENV_PATH` — override `.env` path for the wizard
- `BSS_JAEGER_UI_URL` — `bss trace` UI URL hint

### 3.4 `make` targets

| Target | What it does |
|---|---|
| `help` | Print one-line summary of every public target |
| `up` | `docker compose up -d` — services only (BYOI Postgres/RabbitMQ); pre-creates `.dev-mailbox/` `0777` |
| `up-all` | `docker compose -f docker-compose.yml -f docker-compose.infra.yml up -d` — bundled mode |
| `up-minimal` | `catalog crm payment` only |
| `up-core` | `up-minimal` + `com som subscription provisioning-sim` |
| `down` | Stops everything (both compose files) |
| `build` | `docker compose build` all service images |
| `migrate` | `cd packages/bss-models && uv run alembic upgrade head` (sources `.env` first) |
| `seed` | `uv run bss-seed` — 3 plans (incl. roaming buckets) + 4 VAS (incl. roaming) + 1000 MSISDNs + 1000 eSIM profiles |
| `knowledge-reindex` | (v0.20+) Reindex doc corpus into `knowledge.doc_chunk` for the cockpit's `knowledge.search` tool. Idempotent (mtime + content_hash dedup) |
| `reset-db` | DROP every BSS schema + re-migrate + re-seed (uses `BSS_ALLOW_ADMIN_RESET` paths) |
| `test` | Per-package pytest sweep with PYTHONPATH isolation; excludes `integration` mark |
| `fmt` | `ruff format .` |
| `lint` | `ruff check .` + `mypy .` |
| `scenarios` | Run every `scenarios/*.yaml`. Side-effect: temporarily flips `BSS_PORTAL_EMAIL_PROVIDER → logging`, `BSS_PORTAL_KYC_PROVIDER → prebaked` (+ `BSS_KYC_ALLOW_PREBAKED=true`), `BSS_PAYMENT_PROVIDER → mock` in `.env`, recreates affected containers, restores on EXIT/INT/TERM |
| `scenarios-hero` | Same flip-and-restore, but `--tag hero` (17 hero scenarios) |
| `python-check` | Warn-only check that active Python is 3.12 |
| `check-clock` | Grep guard — every `datetime.now/utcnow` site must route through `bss-clock` |
| `doctrine-check` | Runs `check-clock` plus 13 more grep guards (chat-only orchestrator mediation, OTel-out-of-services, no-campaignos-leak, renewal-reads-snapshot, no-caller-asserted-service-identity, no-runtime-token-env-reads, customer_id-bound-from-state, astream_once-stays-in-chat, stripe-fixtures-redacted, rating-roaming-blind, ported_out-terminal, renewal-worker-lifespan-only, version-from-BSS_RELEASE) |

### 3.5 Cockpit operator preferences (`settings.toml` + `OPERATOR.md`)

The cockpit reads two operator-editable files from `.bss-cli/` (in the cockpit container at `/cockpit-state/`). Both are auto-bootstrapped from `.template` files on first run; both are hot-reloaded on mtime — **no restart needed**.

#### `.bss-cli/settings.toml` (non-secret operator preferences)

```toml
[llm]
# Overrides BSS_LLM_MODEL when set. Comment out to fall back to env.
model = "deepseek/deepseek-v4-pro"
temperature = 0.2

[cockpit]
# Per-turn /confirm overrides this. Keep false unless you know why.
allow_destructive_default = false

[ports]
csr_portal = 9002

[dev_service_urls]
# Optional per-service URL overrides. Empty table = use env defaults.
# crm = "http://localhost:8002"
```

> [!warning] **Don't store secrets in `settings.toml`.** `BSS_*_API_TOKEN`, DB URL, OpenRouter key all stay in `.env`. The greppable rule: anything that would burn the perimeter if leaked stays in env.
>
> `actor` is hardcoded to `"operator"` since v0.13.1 — single-operator-by-design.

#### `.bss-cli/OPERATOR.md` (operator-editable persona)

Prepended **verbatim** to every cockpit system prompt. Use it for house rules, tone, escalation guidance, defaults. Example:

```markdown
# Operator persona

I am Acme Mobile. I run a small MVNO on BSS-CLI and use this cockpit daily.

## House rules

- Use SGD with two-decimal precision in all money references.
- Default to terse, action-first replies.
- For destructive actions, propose first with a one-line summary and wait for `/confirm`.
- Escalations stay on the v0.12 list (fraud, billing dispute, regulator complaint, identity recovery, bereavement).

## Defaults

- Currency: SGD
- Tone: factual, dry, no upsell
```

The cockpit's safety contract (propose-then-confirm, escalation list, ASCII rules) is **code-defined** in `bss_cockpit.prompts._COCKPIT_INVARIANTS`. An operator who wants to weaken the contract has to edit code, not a markdown file.

---

## Part 4 — External providers

BSS-CLI integrates with three external providers across v0.14–v0.16:

| Phase | Provider | Domain |
|---|---|---|
| v0.14 | **Resend** | Transactional email (magic-link login, step-up OTPs, renewal reminders) |
| v0.15 | **Didit** | KYC verification (Singapore eKYC) |
| v0.16 | **Stripe** | Payment tokenization + charging (Stripe Checkout, hosted UI) |

Order is doctrine, not accident: email (lowest stakes; proves the substrate) → KYC (real PII; tests the privacy doctrine) → payment (regulated; tests cutover discipline + PCI scope).

### 4.1 The adapter pattern

All three providers share the v0.14-established adapter pattern:

- **Per-domain `Protocol`** (no unified `Provider.execute()` — see anti-patterns).
- **Pluggable selection** via `select_*` functions that fail-fast at startup on misconfig (no silent fallback to a mock).
- **Env-only configuration** in v0.14–v0.16; tenant-scoped multi-tenant config is post-v0.16.
- **Webhook receivers exempt from `BSSApiTokenMiddleware`** — provider signature is the only auth.
- **Forensic recording** into `integrations.external_call` (outbound) + `integrations.webhook_event` (inbound, idempotent on `(provider, event_id)`).
- **Provider-keyed redaction** via `bss_webhooks.redaction.redact_provider_payload(provider=…, body=…)` before persisting or logging.
- **`bss onboard --domain {email|kyc|payment}`** — interactive wizard that probes credentials with a real provider call, atomically writes `.env`, keeps the most recent 5 `.env.backup-*` rotations.

### 4.2 Resend (transactional email, v0.14)

#### What it does

- Magic-link login (15-min OTP + click-through link)
- Step-up auth (5-min OTP for sensitive actions)
- Email-change verification
- v0.18 renewal reminder (~24h before `next_renewal_at`)

The portal selects an adapter at startup (`bss_portal_auth.email.select_adapter`):
- `LoggingEmailAdapter` writes to `BSS_PORTAL_DEV_MAILBOX_PATH` (dev / scenarios)
- `NoopEmailAdapter` for tests
- `ResendEmailAdapter` for production

The renewal worker reuses the same adapter, so a single env flip drives both.

#### Account setup outside BSS

1. Sign up at [resend.com](https://resend.com).
2. **Add and verify a sending domain** (DKIM, SPF, DMARC). The "From" address (`BSS_PORTAL_EMAIL_FROM`) must be on this domain.
3. **Create an API key.** Best practice: scope it `send-only`. (Note: send-only keys can't call read-only smoke endpoints, so `bss onboard` probes via a real send.)
4. **Create a webhook endpoint** in the dashboard: `POST <BSS_PORTAL_PUBLIC_URL>/webhooks/resend`. Subscribe to: `email.delivered`, `email.bounced`, `email.complained`, `email.failed`, `email.delivery_delayed`. Copy the `whsec_<base64>` signing secret.
5. Free tier: 3k emails/month (sufficient for small MVNO).

#### Env vars

| Var | Required when `provider=resend` |
|---|---|
| `BSS_PORTAL_EMAIL_PROVIDER=resend` | yes |
| `BSS_PORTAL_EMAIL_RESEND_API_KEY` | yes |
| `BSS_PORTAL_EMAIL_FROM` | yes |
| `BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET` | yes |
| `BSS_PORTAL_PUBLIC_URL` | yes (used as base for absolute URLs in email body) |

#### Webhook signature scheme

**svix** — headers `svix-id`, `svix-timestamp`, `svix-signature` (space-separated `v1,<base64>` entries; key rotation supported). Signed payload: `f"{id}.{timestamp}.{body}"`. Verification: `bss_webhooks.signatures.verify_signature(scheme="svix", ...)`.

On signature failure: 401 + `portal_auth.webhook.signature_invalid` structlog (no body persisted on reject).

#### Sandbox affordances

- `BSS_PORTAL_EMAIL_PROVIDER=logging` → `BSS_PORTAL_DEV_MAILBOX_PATH` (default `.dev-mailbox/portal-mailbox.log`). Hero scenarios `tail -f` it for OTP retrieval.
- `BSS_PORTAL_EMAIL_PROVIDER=noop` for tests.
- No special "sandbox key" — Resend's test mode uses the real key.

#### Cutover sandbox → production

`bss onboard --domain email` walks: mode (test/production), Resend API key (must start `re_`), `BSS_PORTAL_EMAIL_FROM`, webhook secret (warns if not `whsec_*`), then optionally probes by sending a real test email. Restart the portal container.

#### Failure modes

- Send error → `portal_auth.email.send_failed` structlog; auth flow surfaces a generic "couldn't send code, try again" templated response.
- `email.bounced` / `email.complained` webhooks land in `integrations.webhook_event`, visible via `bss external-calls`.

#### PCI/PDPA

- `redact_provider_payload(provider="resend", ...)` masks `to`/`from`/`reply_to`/`cc`/`bcc` before persistence.
- structlog carries `email_domain` only — never the full address, never the OTP.

### 4.3 Didit (KYC verification, v0.15)

#### What it does

Hosted-UI flow for document capture, liveness, biometric matching. BSS receives **only a verification receipt** (`KycAttestation`):
- provider, provider reference
- document type / country
- `document_number_last4` + `sha256(number|country|provider)` (domain-separated)
- DOB
- `corroboration_id` pointing at an HMAC-verified webhook row

Names, addresses, biometric URLs, MRZ data, raw document number, full DOB-as-string **never cross the BSS boundary**.

> [!warning] **Trust anchor.** Didit's `GET /v2/session/{id}/decision/` API is plain JSON over TLS with **no JWS signature** (probed 2026-05-02). The HMAC-signed inbound webhook is therefore the trust anchor. `DiditKycAdapter.fetch_attestation` blocks on the corroboration row before returning. Policy `check_attestation_signature` rejects (`rule=kyc.attestation.uncorroborated`) any `provider="didit"` attestation whose `corroboration_id` doesn't resolve to a fresh (≤30 min) `Approved` row in `integrations.kyc_webhook_corroboration`.

#### Free-tier cap

500 sessions/month, hard. `DiditKycAdapter.initiate` queries `integrations.external_call` for the running monthly count and raises `KycCapExhausted` at 500. Warning logged at 450. **No silent fallback to prebaked** — customer sees a templated retry page; ops gets a high-priority `kyc.cap_exhausted` event.

#### Account setup outside BSS

1. Sign up at [didit.me](https://didit.me).
2. **Create a workflow** (KYC verification). The workflow ID is a raw UUID (`8-4-4-4-12 hex`, no `wf_` prefix; onboard validates via regex).
3. **Generate an API key** (used as `x-api-key` header).
4. **Create a webhook endpoint** in the dashboard: `POST <BSS_PORTAL_PUBLIC_URL>/webhooks/didit`. Copy the signing secret. Without this secret the BSS policy treats every attestation as uncorroborated.
5. Sandbox uses the same dashboard; test mode returns a stable test NRIC (`S8369796B`-shaped) on every verification — see `BSS_KYC_ALLOW_DOC_REUSE`.

#### Env vars

| Var | Required when `provider=didit` |
|---|---|
| `BSS_PORTAL_KYC_PROVIDER=didit` | yes |
| `BSS_PORTAL_KYC_DIDIT_API_KEY` | yes |
| `BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID` | yes (raw UUID) |
| `BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET` | yes |
| `BSS_PORTAL_PUBLIC_URL` | yes (used as base for `return_url` passed to `KycVerificationAdapter.initiate()`) |
| `BSS_KYC_ALLOW_PREBAKED` | sandbox-only (refused in production unless explicit) |
| `BSS_KYC_ALLOW_DOC_REUSE` | sandbox-only (refused in production) |
| `BSS_ENV=production` | refused if `provider=prebaked` and `ALLOW_PREBAKED` not explicit |

#### Webhook signature scheme

**didit_hmac** — header `X-Signature-V2` (or alias `X-Signature`) carrying hex HMAC-SHA-256, plus `X-Timestamp` (unix seconds). The signature is over **body alone** (timestamp NOT mixed in — verified offline against three real deliveries). Replay protection therefore depends on timestamp freshness window (300s) plus the unique constraint on `(provider, provider_session_id)` in `integrations.kyc_webhook_corroboration`.

Body discriminator: Didit Webhooks v3.0 uses `webhook_type` (`status.updated`, `data.updated`, …). Older drafts read `type`/`event` — both wrong; current handler accepts all three.

Multi-event progression: each session lifecycle (`Not Started → In Progress → Approved`) emits multiple webhooks with the **same** `session_id` but distinct `event_id`. Handler dedupes on `(provider, event_id)` for retries but updates the corroboration row's `decision_status` last-write-wins so the final `Approved` lands.

#### Sandbox affordances

- `BSS_PORTAL_KYC_PROVIDER=prebaked` returns a deterministic per-email attestation, no external call. Used by 14-day soak corpus + hero scenarios.
- `BSS_KYC_ALLOW_PREBAKED=true` admits these in production. **Dangerous; only set if you've consciously decided to.**
- `BSS_KYC_ALLOW_DOC_REUSE=true` lets the same hash re-link to the latest customer (prior identity row dropped). **Required for any multi-customer sandbox testing against Didit** because Didit sandbox returns the same NRIC every test session.

#### Cutover sandbox → production

```bash
bss onboard --domain kyc
# Walks: mode (test=prebaked / production=didit), API key, workflow ID UUID
# regex-validation, webhook secret, then optionally probes by minting a real
# sandbox session and printing the redirect URL.
```

`select_kyc_adapter` fail-fast at portal startup: missing API key, missing workflow ID, missing session_factory all raise `RuntimeError`.

Final cutover: `BSS_PORTAL_KYC_PROVIDER=didit`, the three `BSS_PORTAL_KYC_DIDIT_*` vars, `BSS_KYC_ALLOW_PREBAKED=false`, `BSS_KYC_ALLOW_DOC_REUSE=false`, `BSS_ENV=production`, restart portal.

#### Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `KycCapExhausted` | 500/mo Didit limit hit | No fallback by design. Investigate monthly billing or upgrade tier |
| `KycCorroborationTimeout` (10s default poll) | Webhook hasn't arrived | Customer sees retry page; webhook usually arrives on next click |
| `kyc.attestation.uncorroborated` | corroboration_id missing/malformed/not-Approved/older than 30min | Investigate webhook delivery |
| `customer.attest_kyc.document_hash_unique_per_tenant` | Duplicate hash | If sandbox: set `BSS_KYC_ALLOW_DOC_REUSE=true` |

#### PCI / PDPA

- **PII reduction point: `_build_attestation` in `kyc/didit.py`.** This is the doctrine boundary. After this returns, raw doc number, names, addresses, image URLs are gone.
- Greppable rule: `rg '(first_name|last_name|address|portrait_image|...)' services/crm/app/ portals/self-serve/bss_self_serve/kyc/didit.py` matches **only** the redaction call site (`_build_attestation`).
- Document number reduced to `last4` + `sha256(number|country|provider)` (domain-separated).
- `redact_provider_payload(provider="didit")` is defense-in-depth: hashes raw doc numbers/DOB, masks names — recorded `integrations.external_call.redacted_payload` should never carry plaintext.

### 4.4 Stripe (payment tokenization + charging, v0.16)

#### What it does

`StripeTokenizerAdapter` (in `services/payment/app/domain/stripe_tokenizer.py`) implements the `TokenizerAdapter` Protocol against Stripe's REST API via the official `stripe` Python SDK (sync; wrapped in `asyncio.to_thread`).

**Operations:**
- `charge` — Off-session, `confirm=True` `PaymentIntent.create` against an attached customer. Returns `ChargeResult` with status `approved`/`declined`/`errored` plus `provider_call_id` (`pi_*`) and `decline_code` for declines.
- `tokenize` — **Raises `NotImplementedError` in production.** PAN never touches BSS. Server-side tokenization is a security mistake.
- `attach_payment_method_to_customer` — Used during signup/COF-add; in sandbox handles `payment_method_already_attached` via detach-then-reattach when `BSS_PAYMENT_ALLOW_TEST_CARD_REUSE=true`.
- `ensure_customer` — Lazy-creates `cus_*` keyed by BSS customer id, cached in `payment.customer.customer_external_ref`.
- `retrieve_payment_method_card` — Reads canonical `last4`/`brand`/`exp` from Stripe.

**Card-on-file flow — Stripe Checkout (hosted UI):**
1. Portal mints `stripe.checkout.Session.create(mode="setup", ...)` server-side using its **secret** key (so no publishable key in the browser, no Stripe.js, no iframe).
2. Customer 303-redirected to Stripe's hosted card form.
3. Stripe redirects back to `/payment-methods/add/checkout-return` (or `/signup/step/cof/checkout-return`) with `cs_id`. Portal retrieves the SetupIntent, extracts `pm_*`, registers via `bss-clients` with `tokenization_provider="stripe"`.

> [!warning] **Trust hierarchy.** Synchronous charge response is **primary** source of truth for `payment_attempt.status`. Webhook is **secondary** — reconciles and detects drift (`payment.attempt_state_drift` event), never overwrites the row. Multiple webhooks per logical state transition (`charge.created` → `payment_intent.processing` → `charge.succeeded`) are deduped on `(provider, event_id)` but the receiving row updates every webhook (last-write-wins).

#### Account setup outside BSS

1. Sign up at [stripe.com](https://stripe.com). Activate the account (live mode requires business verification).
2. **Get API keys:** Dashboard → Developers → API keys. Note both **secret** (`sk_test_*` or `sk_live_*`) and **publishable** (`pk_test_*` / `pk_live_*`) — they must be the same mode.
3. **Configure Checkout:** no extra setup; just enable card payments in Dashboard → Payment methods.
4. **Create a webhook endpoint:** Dashboard → Developers → Webhooks → Add endpoint at `<BSS_PAYMENT_WEBHOOK_PUBLIC_URL>/webhooks/stripe`. Subscribe to: `charge.succeeded`, `charge.failed`, `payment_intent.payment_failed`, `charge.refunded`, `charge.dispute.created`. Copy the `whsec_*` signing secret.
5. **Test cards:** `4242 4242 4242 4242` (Visa, succeeds), `4000 0000 0000 0002` (declined), `4000 0027 6000 3184` (3DS challenge), etc. Stripe sandbox provides one shared `pm_card_visa` per account — see `BSS_PAYMENT_ALLOW_TEST_CARD_REUSE`.

#### Env vars

| Var | Required when `provider=stripe` |
|---|---|
| `BSS_PAYMENT_PROVIDER=stripe` | yes |
| `BSS_PAYMENT_STRIPE_API_KEY` (`sk_test_*` or `sk_live_*`) | yes |
| `BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY` (must match secret-key mode) | yes |
| `BSS_PAYMENT_STRIPE_WEBHOOK_SECRET` (`whsec_*`) | yes — non-negotiable |
| `BSS_PAYMENT_ALLOW_TEST_CARD_REUSE` | sandbox-only; refused if `sk_live_*` |
| `BSS_ENV=production` | triggers `sk_test_*` refusal + PCI template scan |

#### Webhook signature scheme

**stripe** — header `Stripe-Signature` carrying `t=<timestamp>,v1=<hex>,v1=<hex>...` (key rotation supported). Signed payload: `f"{timestamp}.{body}"`. 300s freshness window.

> [!info] **Diagnostic logging on signature_invalid is shipped from day 1** (response to a v0.15 lesson where three Didit deliveries failed silently with `malformed_header`). Every reject logs `candidate_headers` (header keys + values truncated to 80 chars; auth/secret/token headers redacted) AND `body_preview` (first 500 chars). The webhook secret never appears in logs (verified by test).

#### Webhook routing logic (`webhook_stripe`)

| Event | Handler | What happens |
|---|---|---|
| `charge.succeeded` / `charge.failed` / `payment_intent.payment_failed` | `_route_terminal_charge` | Looks up `payment_attempt.provider_call_id == pi_*`. If row matches → `outcome=reconciled`. If contradicts → emits `payment.attempt_state_drift`, **never overwrites** |
| `charge.refunded` | `_route_refund` | Emits `payment.refunded` with `amount_refunded_minor`. **No automatic balance adjustment** (motto #1: bundled-prepaid; refund is an exception). Operator handles |
| `charge.dispute.created` | `_route_dispute` | Emits `payment.dispute_opened` for the cockpit to surface. **No auto-case-creation, no service block.** Operator decides |
| `payment_intent.created`, `charge.created`, etc. | persisted forensic | `outcome=noop` |

#### Sandbox affordances

- `BSS_PAYMENT_PROVIDER=mock` — in-process tokenizer, all hero scenarios. Card tokens are `tok_<uuid>`. Test affordances: `tok_FAIL_*` (errors), `tok_DECLINE_*` (declined).
- `BSS_PAYMENT_ALLOW_TEST_CARD_REUSE=true` (sandbox-only; refused with `sk_live_*` at startup) — handles `payment_method_already_attached` by detach-then-reattach.
- `BSS_ENABLE_TEST_ENDPOINTS=true` — exposes `/dev/*` tokenizer for the dev `bss payment add-card` CLI; off by default.
- Three-provider soak (`scenarios/live_sandbox/`) — gated on `BSS_NIGHTLY_SANDBOX=true`. **Refuses `sk_live_*`** (non-negotiable safety belt). Five tiers; three consecutive green runs = release-tag confidence.

#### Cutover sandbox → production

**See [§8.5](#85-stripe-cutover-mock--production)** for the full runbook. Two paths:
1. **Lazy-fail (default)** — `payment.charge.token_provider_matches_active` policy raises on every charge.
2. **Proactive (`bss payment cutover --invalidate-mock-tokens`)** — Mass-invalidates mock tokens; recommended.

**Four startup guards** in `select_tokenizer` (all enforced before FastAPI accepts traffic):
1. Unknown provider name → `RuntimeError`.
2. `stripe` + missing `STRIPE_API_KEY` / `STRIPE_PUBLISHABLE_KEY` / `STRIPE_WEBHOOK_SECRET` → `RuntimeError` with the specific missing-var name.
3. `BSS_ENV=production` + `sk_test_*` (or mixed test/live mode) → refuses.
4. `ALLOW_TEST_CARD_REUSE=true` + `sk_live_*` → refuses.

**PCI template scan** (`pci_scope.py`) runs in production-stripe mode at portal startup; refuses to boot if any non-`*_mock.html` template contains `<input name="card_number">` (or `pan`, `cardNumber`).

#### Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `stripe.error.CardError` | Issuer decline | Recorded as normal `declined`. `decline_code` + `pi_*` captured. `payment.declined` event |
| Non-`succeeded` sync status | Unexpected | Treated as `errored`; row stays recoverable for webhook reconciliation |
| `payment.charge.token_provider_matches_active` | Cutover lazy-fail | Tells operator to run cutover or accept lazy-fail with comms |
| `payment.attempt_state_drift` | Webhook contradicts row | Investigate; do **not** overwrite row |
| `charge.dispute.created` | Chargeback | Record-only. Operator decides whether to open a case |
| `charge.refunded` (out-of-band) | Dashboard refund | Record-only. Operator initiates balance reversal manually |

#### PCI / PDPA

- **PCI scope: SAQ A.** Achieved by Stripe Checkout (hosted UI). PAN never touches BSS. Server-side `tokenize` raises `NotImplementedError`. PCI template scan refuses to boot the portal in prod with a `card_number` input.
- Webhook diagnostic logs redact `secret`/`token`/`authorization` headers.
- `redact_provider_payload(provider="stripe")` masks `email`/`name`/`phone`/`address`/`billing_details` before persistence.
- **Idempotency keys** are `ATT-{attempt_id}-r0` (one attempt → one key). Reusing a key across user-initiated retries is a doctrine bug — same key only on BSS-crash-restart retries (post-v0.16 path). See [§8.17](#817-payment-idempotency-forensics).

---

## Part 5 — Operator guide

### 5.1 The REPL (`bss`) — canonical cockpit

**Entry:** `bss` (Typer root) — invoke with no subcommand to enter the cockpit REPL.

- **No login.** `actor` is read from `.bss-cli/settings.toml` (descriptive, not verified). The cockpit runs single-operator-by-design behind a secure perimeter.
- **Flags:** `--session SES-NNN` resume, `--new` force fresh, `--label "..."`, `--list` (print sessions and exit), `--allow-destructive` (default-off; flips destructive gating off for the whole session).
- **`.env` auto-bootstrap:** root `bss` reads repo `.env` into `os.environ` if not already exported.
- **History:** persistent under `.bss-cli/repl_history` (prompt_toolkit; Up/Down + Ctrl-R).

**Two interaction modes** inside the REPL:
1. **Direct Typer subcommands** — deterministic, no LLM. See [§5.3](#53-direct-typer-subcommands-every-leaf).
2. **Natural-language input** — sent through `astream_once(tool_filter="operator_cockpit", service_identity="operator_cockpit")`. **Loose-substring intent intercepts** (`repl.py:_INTENT_RULES`) deterministically dispatch list/show requests *before* the LLM (e.g. "list customers" → `customer.list`; "show port requests" → `port_request.list`) — same renderer dispatch as the LLM-mediated path, faster and zero-token.

### 5.2 Browser cockpit (`localhost:9002`)

**Entry:** `http://localhost:9002/`. **No login route, no middleware-level auth.** Single-operator-by-design.

Same `Conversation` store as the REPL — exit `bss`, open `/cockpit/<id>`, see the same turns.

**Routes** (`portals/csr/bss_csr/routes/`):

| Route | What |
|---|---|
| `GET /` | Sessions index (recent + new-conversation CTA + customer search) |
| `POST /cockpit/new` | Open a fresh session, 303 to it |
| `GET /cockpit/{session_id}` | Chat-thread page; renders structured `cockpit.message` rows |
| `POST /cockpit/{session_id}/turn` | Append user message |
| `POST /cockpit/{session_id}/reset` | Clear messages (keeps row) |
| `POST /cockpit/{session_id}/confirm` | Marker; next turn consumes pending destructive row |
| `POST /cockpit/{session_id}/focus` | Set/clear customer focus |
| `GET /cockpit/{session_id}/events` | SSE streaming turn |
| `GET /search?q=...` | Customer search by name or MSISDN |
| `POST /search/start_session` | Open fresh session pre-pinned to a customer |
| `GET /case/{case_id}` | Read-only case thread; renders chat transcript when escalation-bound |
| `GET /settings`, `POST /settings/operator`, `POST /settings/config` | View / save `OPERATOR.md` + `settings.toml` |

**Slash command parity** is a doctrine target: the browser surface has buttons for `/confirm`, `/focus`, `/reset`, `/new`. Slash commands typed in the message box (e.g. `/confirm`) are intercepted in the SSE handler.

### 5.3 Direct Typer subcommands (every leaf)

Source: `cli/bss_cli/commands/`.

| Group | Commands |
|---|---|
| `bss customer` | `create`, `list`, `show <id>` |
| `bss case` | `open`, `list`, `show <id>`, `close` |
| `bss ticket` | `open`, `list`, `show`, `assign`, `ack`, `start`, `resolve`, `close`, `cancel` |
| `bss order` | `create`, `show`, `list`, `cancel` |
| `bss subscription` | `show`, `list`, `vas` (purchase top-up), `renew`, `terminate` |
| `bss catalog` | `list` (active offerings), `vas` (list VAS), `show <id>` |
| `bss payment` | `add-card`, `list-methods`, `remove-method`, `cutover` (Stripe migration: invalidate mock tokens) |
| `bss usage` | `simulate` (TMF635 mediation event; `--roaming` flag) |
| `bss prov` | `tasks`, `show`, `resolve` (resolve stuck), `retry` (retry failed), `fault` (set fault injection) |
| `bss som` | `service list`, `service-show`, `so-show` |
| `bss inventory` | `msisdn list/show/add-range`, `esim list/show/activation` |
| `bss clock` | `now`, `advance` (deterministic time for scenarios) |
| `bss trace` | `get`, `for-order`, `for-subscription`, `for-ask`, `services` |
| `bss admin` | `reset` (operational data reset — destructive); subgroup `bss admin catalog`: `add-offering`, `set-price`, `window-offering`, `migrate-price`, `show` |
| `bss scenario` | `validate`, `list`, `run`, `run-all` (YAML scenario runner) |
| `bss onboard` | Provider config wizard (`--domain email \| kyc \| payment`) |
| `bss external-calls` | Read-only browser over `integrations.external_call`; `--provider`, `--since`, `--aggregate IDT-NNNN`, `--month-to-date`, `--limit`, `--failures` |
| `bss ask "..."` | Single-shot LLM dispatch; `--allow-destructive` to bypass gating |

### 5.4 Slash commands

Source: `cli/bss_cli/repl.py:_handle_slash`.

| Command | What |
|---|---|
| `/sessions` | Rich table of operator's recent cockpit sessions |
| `/new [LABEL]` | Close current → open new (label optional) |
| `/switch SES-NNN` | Resume specific session id; prints last 5 turns |
| `/reset` | Clear messages on current session (keeps row) |
| `/focus CUST-NNN` | Pin a customer for the system prompt |
| `/focus clear` | Unset focus |
| `/360 [CUST-NNN]` | Render customer 360 (customer + subs + cases + interactions); persists as a tool turn (uses pinned focus if no arg) |
| `/ports` | Operator MNP queue: `list` (default), `approve PORT-NNN`, `reject PORT-NNN <reason>` |
| `/confirm` | Flip next turn `allow_destructive=True`; consumes pending `cockpit.pending_destructive` row |
| `/config edit` | Open `.bss-cli/settings.toml` in `$EDITOR`; mtime hot-reload |
| `/operator edit` | Open `.bss-cli/OPERATOR.md` in `$EDITOR`; mtime hot-reload |
| `/help` | List slash commands |
| `/exit`, `/quit` | Leave (does not close the session) |

### 5.5 Cockpit LLM tools (`operator_cockpit` profile)

Source: `orchestrator/bss_orchestrator/tools/_profiles.py`. **Coverage assertion** (registry minus `*.mine` wrappers); startup `validate_profiles()` enforces.

#### Reads

`customer.get`, `customer.list`, `customer.find_by_msisdn`, `customer.get_kyc_status`, `case.get`, `case.list`, `case.show_transcript_for`, `ticket.get`, `ticket.list`, `interaction.list`, `catalog.list_active_offerings`, `catalog.list_offerings`, `catalog.get_offering`, `catalog.get_active_price`, `catalog.list_vas`, `catalog.get_vas`, `subscription.list_for_customer`, `subscription.get`, `subscription.get_balance`, `subscription.get_esim_activation`, `service.get`, `service.list_for_subscription`, `order.get`, `order.list`, `order.wait_until`, `service_order.get`, `service_order.list_for_order`, `payment.list_methods`, `payment.list_attempts`, `payment.get_attempt`, `inventory.msisdn.list_available`, `inventory.msisdn.count`, `inventory.msisdn.get`, `inventory.esim.list_available`, `inventory.esim.get_activation`, `provisioning.get_task`, `provisioning.list_tasks`, `usage.history`, `trace.get`, `trace.for_order`, `trace.for_subscription`, `events.list`, `agents.list`, `clock.now`.

#### Writes

| Domain | Tools |
|---|---|
| CRM | `customer.create`, `customer.update_contact`, `customer.add_contact_medium`, `customer.remove_contact_medium`, `customer.attest_kyc`, `customer.close`, `interaction.log` |
| Cases / tickets | `case.open`, `case.close`, `case.add_note`, `case.transition`, `case.update_priority`, `ticket.open`, `ticket.assign`, `ticket.transition`, `ticket.resolve`, `ticket.close`, `ticket.cancel` |
| Catalog admin | `catalog.add_offering`, `catalog.add_price`, `catalog.window_offering` |
| Subscription | `subscription.terminate`, `subscription.schedule_plan_change`, `subscription.cancel_pending_plan_change`, `subscription.migrate_to_new_price`, `subscription.purchase_vas`, `subscription.renew_now`, `subscription.tick_renewals_now` |
| Orders | `order.create`, `order.cancel` |
| Payment | `payment.add_card`, `payment.remove_method`, `payment.charge` |
| Inventory | `inventory.msisdn.add_range` |
| **Port-request (operator-only)** | `port_request.list`, `port_request.get`, `port_request.create`, `port_request.approve`, `port_request.reject` |
| Provisioning ops | `provisioning.resolve_stuck`, `provisioning.retry_failed`, `provisioning.set_fault_injection` |
| Test/scenario | `clock.advance`, `clock.freeze`, `clock.unfreeze`, `usage.simulate` |
| **Knowledge (v0.20+)** | `knowledge.search`, `knowledge.get` — RAG over the indexed doc corpus. **Operator-cockpit only**; customer chat does NOT receive these by doctrine |

> [!info] **The operator cannot call `*.mine` / `*_for_me` wrappers** — those are customer-chat-only (validated at startup; the profile rejects mine wrappers). The operator IS the trust principal here.

### 5.6 Destructive-tool gating (`/confirm`)

Source: `orchestrator/bss_orchestrator/safety.py:DESTRUCTIVE_TOOLS`.

```
customer.close, customer.remove_contact_medium, case.close, ticket.cancel,
payment.remove_method, order.cancel, subscription.terminate,
subscription.terminate_mine, provisioning.set_fault_injection,
admin.reset_operational_data, admin.force_state
```

**Flow:**
1. LLM proposes a destructive call (one-line summary + tool name + arguments).
2. `cockpit.pending_destructive` row inserted; `wrap_destructive` short-circuits with `{"error": "DESTRUCTIVE_OPERATION_BLOCKED", ...}` if the flag isn't set.
3. Operator types `/confirm` (REPL) or clicks the button (browser).
4. Next turn runs `allow_destructive=True` and the proposal payload is pinned into the system prompt (`build_cockpit_prompt`'s "Confirmed destructive action" block).

> [!warning] **An LLM that proposes without `/confirm` pairing** → REPL stashes the proposal and shows `[Pending /confirm for tool_name — type /confirm to authorise the next turn.]`. If you don't `/confirm`, the proposal evaporates on the next turn (no auto-execute on follow-up).
>
> Direct Typer subcommands (`bss subscription terminate`, etc.) **bypass the LLM gate** but still flow through service-side policy.

---

## Part 6 — Customer guide

### 6.1 Self-serve portal (`localhost:9001`)

**Entry:** `http://localhost:9001/`.

**Auth:** email-based identity (`bss-portal-auth`). Cookie-only; sessions are server-side; tokens HMAC-SHA-256 with `BSS_PORTAL_TOKEN_PEPPER`. Step-up auth gates sensitive writes.

**Public allowlist** (`portals/self-serve/bss_self_serve/security.py`): `/welcome`, `/plans`, `/auth/*`, `/static/*`, `/portal-ui/static/*`, `/terms`, `/privacy`, `/signup/step/kyc/callback`, `/webhooks/*`. Everything else gates on session.

#### Capabilities (categorized)

| Category | What |
|---|---|
| Auth + identity | Start email login, verify magic link / OTP, step-up auth, logout |
| Browse / account | Dashboard (line cards, balances), browse plans, legal pages |
| Sign up | Pick plan → KYC attestation (Didit hosted UI in prod) → COF (Stripe Checkout in prod) → place order → poll activation |
| Subscription | View subscriptions, balances, plan details; re-display LPA activation code (eSIM); view billing history |
| Plan change | Schedule a switch (next-renewal pivot — no proration); cancel a pending plan change |
| Top-up / VAS | Purchase a top-up (charges card on file); roaming top-up (`VAS_ROAMING_1GB`) materializes balance row if missing |
| Cancel a line | Terminate a subscription (irreversible; step-up gated) |
| Profile | Update display name, phone, postal address; change verified email (OTP flow) |
| Payment methods | Add card, remove card, set default |
| Chat | See [§6.2](#62-customer-chat-the-only-orchestrator-mediated-route) |

#### Cannot do

- Pass `customer_id` / `subscription_id` / `service_id` / `payment_method_id` as user-controllable input on **any** post-login route. `customer_id` reads from `request.state.customer_id` only; ownership policies enforce server-side.
- Compose multiple writes in a single route (anti-pattern v0.10). Each route is one `bss-clients` call. The cross-schema email-change flow is the only documented exception.
- MNP port-out / port-in self-serve (operator-only by spec — `port_request.*` lives in `operator_cockpit`).
- Real eSIM redownload re-arm (v0.10 read-only re-display; SM-DP+ is simulated).
- Open a case directly via portal — there is no "open a case" page; the five escalation categories route through chat.
- Bypass step-up — every label in `SENSITIVE_ACTION_LABELS` gates via `requires_step_up(label)`; tested.

For the full route list see [§10.5](#105-portal-routes-every-one).

### 6.2 Customer chat (the only orchestrator-mediated route)

**Entry:**
- `GET /chat` — full standalone page
- `GET /chat/widget` — bottom-right popup partial (loaded by every post-login page via HTMX FAB)
- `POST /chat/message` — submit a turn
- `POST /chat/reset` — clear running conversation
- `GET /chat/events/{session_id}` — SSE stream

> [!warning] **Greppable doctrine guard:** `rg 'astream_once' portals/self-serve/bss_self_serve/routes/` must match `chat.py` only. Chat is the **only** orchestrator-mediated route in self-serve.

#### Capabilities (`customer_self_serve` tool profile)

Source: `orchestrator/bss_orchestrator/tools/_profiles.py`. **Every entry must have a matching `OWNERSHIP_PATHS` entry**; startup `validate_profiles()` enforces.

| Category | Tools |
|---|---|
| Public catalog reads | `catalog.list_vas`, `catalog.list_active_offerings`, `catalog.get_offering` |
| Read `*.mine` wrappers | `subscription.list_mine`, `subscription.get_mine`, `subscription.get_balance_mine`, `subscription.get_lpa_mine`, `usage.history_mine`, `customer.get_mine`, `payment.method_list_mine`, `payment.charge_history_mine` |
| Write `*.mine` wrappers | `vas.purchase_for_me`, `subscription.schedule_plan_change_mine`, `subscription.cancel_pending_plan_change_mine`, `subscription.terminate_mine` (still in `DESTRUCTIVE_TOOLS`) |
| Escalation | `case.open_for_me` |
| Case lookup | `case.list_for_me` |

#### Three layers of containment

1. **Server-side policies** (primary) — every CRM/payment/subscription policy still applies, regardless of how the call originated.
2. **Pre-flight `*.mine` wrappers** — bind `customer_id` from `auth_context.current().actor`; never accept it as a parameter (`FORBIDDEN_MINE_PARAMETERS = {customer_id, customer_email, msisdn}`).
3. **Output trip-wire** — `OWNERSHIP_PATHS` lists JSON paths in each tool's response that must equal the bound actor; mismatch raises `AgentOwnershipViolation`, caught by chat route → generic safety reply (no leaked tool name).

#### Cannot do

- **Cannot accept owner-bound parameter** on `*.mine` / `*_for_me` tools.
- **Cannot escape its profile.** Cap-trip → templated SSE response, never a raw error. The OpenRouter API key never leaves the orchestrator process.
- **Cannot help outside the five non-negotiable escalation categories.** Adding a sixth is a doctrine decision.
- **Cannot promise turnaround time.** Case-event emails are operator-driven; quantified time windows are forbidden in the prompt.
- **Cannot help anonymous (verified-email-only, not-yet-customer) visitors** beyond explaining plans and pointing at `/plans`.
- **Cannot tell the customer to "email support" for things the portal can do.** The system prompt has an explicit redirect map for self-serve flows.

#### The five non-negotiable escalation categories

`fraud`, `billing_dispute`, `regulator_complaint`, `identity_recovery`, `bereavement`. Encoded **identically** in:
- `EscalationCategory` Literal in `orchestrator/bss_orchestrator/types.py`
- v0.12 soak corpus
- Customer-chat system prompt (`customer_chat_prompt.py`)
- OPERATOR.md template

Adding a sixth is a doctrine decision, not a scope decision.

#### Anti-hallucination guard

`_RE_ESCALATION_CLAIM` in `routes/chat.py` regex-detects first-person active escalation language ("I've escalated this", "I'm raising a case") and verifies `case.open_for_me` actually fired this turn. If not, the reply is replaced with `_ESCALATION_HALLUCINATION_FALLBACK` pointing at `BSS_OPERATOR_SUPPORT_EMAIL`.

### 6.3 Step-up auth labels

Source: `portals/self-serve/bss_self_serve/security.py:SENSITIVE_ACTION_LABELS`.

```
vas_purchase, payment_method_add, payment_method_remove, payment_method_set_default,
subscription_terminate, email_change, phone_update, address_update, name_update,
plan_change_schedule, plan_change_cancel
```

**Cross-checked:** every label appears in at least one `requires_step_up(...)` call site, every call site uses one of these. Adding a new sensitive route requires adding its label here.

---

## Part 7 — Domain features

### 7.1 Catalog — offerings, VAS, allowances, roaming

#### Concepts

- **Product Offering** (`catalog.product_offering`) — a sellable product (`PLAN_S`, `PLAN_M`, `PLAN_L`, `VAS_DATA_1GB`, etc.). TMF620.
- **Product Offering Price** (`catalog.product_offering_price`) — recurring or one-shot price; many-prices-per-offering supported (with date windows).
- **Bundle Allowance** (`catalog.bundle_allowance`) — a quantity of an `allowance_type` for a recurring plan: `data` (mb), `voice` (minutes), `voice_minutes` (alias), `sms` (count), `data_roaming` (mb). Quantity `-1` means unlimited.
- **VAS Offering** (`catalog.vas_offering`) — one-shot top-ups with their own allowance spec (e.g. `VAS_ROAMING_1GB`).

#### Seeded data (3 plans + 4 VAS)

| Offering | Data | Voice | SMS | Roaming | Price |
|---|---|---|---|---|---|
| `PLAN_S` | 5 GB | 100 min | 100 | 0 mb (none) | SGD 19 |
| `PLAN_M` | 30 GB | unlimited | unlimited | 500 mb | SGD 39 |
| `PLAN_L` | 150 GB | unlimited | unlimited | 2 GB | SGD 79 |
| `VAS_DATA_1GB` | +1 GB | — | — | — | SGD 3 |
| `VAS_DATA_5GB` | +5 GB | — | — | — | SGD 12 |
| `VAS_UNLIMITED_DAY` | unlimited (24h) | — | — | — | SGD 5 |
| `VAS_ROAMING_1GB` | — | — | — | +1 GB | SGD 8 |

> [!info] **Roaming is additive.** PLAN_S has zero included roaming, but a customer can still buy `VAS_ROAMING_1GB` — the subscription **materializes** a fresh `data_roaming` `BundleBalance` row on first top-up.

#### Snapshot pricing doctrine (v0.7+)

Subscription price is **snapshotted at order time** onto the subscription row (`price_amount`, `price_currency`, `price_offering_price_id`). Renewal charges the snapshot, **not** the catalog. Catalog price changes affect new orders only; existing subscriptions migrate via the explicit operator-initiated flow `subscription.migrate_to_new_price` — see [§8.3](#83-migrate-customers-to-a-new-price).

### 7.2 Customer signup with KYC

#### Five-step deterministic chain

`portals/self-serve/bss_self_serve/routes/signup.py`. Each step is one `bss-clients` write call (or one read).

| Step | Route | What |
|---|---|---|
| 0 | `GET /signup/{plan}` | Form, gated by `requires_verified_email` |
| 1 | `POST /signup` | `crm.create_customer` + `link_to_customer`. Returning-customer short-circuit: existing identity → reuse `customer_id`, jump to order |
| 2 | `POST /signup/step/kyc` | Prebaked: sync. Didit: renders QR + 3s desktop poll. `GET /signup/step/kyc/poll` polls `kyc_webhook_corroboration` for terminal state. `GET /signup/step/kyc/callback` is the public "thanks, return to your computer" page |
| 3 | `POST /signup/step/cof` (mock) **OR** `POST /signup/step/cof/checkout-init` + `GET /signup/step/cof/checkout-return` (Stripe Checkout) | Card-on-file |
| 4 | `POST /signup/step/order` | `com.create_order` + `submit_order` |
| 5 | `GET /signup/step/poll` | Read-only poll on `com.get_order`; on `state==completed` extracts `targetSubscriptionId` and emits HX-Redirect to `/confirmation/{sub_id}` |

#### Trust + privacy

- `crm.kyc_attestation` carries `document_number_last4` + `document_number_hash` only.
- `integrations.kyc_webhook_corroboration` is the trust anchor for Didit attestations.
- Policy `customer.attest_kyc.attestation_signature_valid` rejects uncorroborated attestations (provider="didit") with rule `kyc.attestation.uncorroborated`.
- `BSS_KYC_ALLOW_PREBAKED` controls non-Didit attestations in production.
- `BSS_KYC_ALLOW_DOC_REUSE` bypasses uniqueness for sandbox testing.

#### Operator gotchas

- "KYC stuck on QR for 10 min" → check `integrations.kyc_webhook_corroboration` for the provider session id; row missing means Didit never reached us (webhook secret mismatch or upstream retries — see [§8.6](#86-three-provider-sandbox-soak-release-gate)).
- "Cap exhausted" means 500/mo Didit limit hit; **no fallback by design**. Customer sees retry page; ops investigates monthly billing or upgrades tier.
- Returning-customer 2nd-line path: POST `/signup` detects `identity.customer_id`, skips create-customer + KYC + COF, jumps to `pending_order`. Audit row `signup.create_customer.reused_linked_identity` is expected.

### 7.3 Payment methods + charging

#### Data model

| Table | Purpose |
|---|---|
| `payment.payment_method` | `customer_id`, `token`, `last4`, `brand`, `is_default`, `tokenization_provider`, `expires_at` |
| `payment.payment_attempt` | `customer_id`, `payment_method_id`, `amount`, `currency`, `purpose` (activation/renewal/vas), `status` (approved/declined/errored), `gateway_ref`, `provider_call_id` (`pi_*`), `decline_code`, `idempotency_key` |
| `payment.customer` | Per-(BSS customer, provider) cache of `customer_external_ref` (`cus_*`); v0.16 add |
| `integrations.webhook_event` | Every Stripe webhook persisted (provider, event_id, body, signature_valid, outcome, processed_at). Idempotent on `(provider, event_id)` |

#### Policies (`services/payment/app/policies/`)

- `payment.charge.method_active`
- `payment.charge.positive_amount`
- `payment.charge.customer_matches_method`
- `payment.charge.token_provider_matches_active` — v0.16 lazy-fail cutover guard
- `payment_method.add.{customer_exists, customer_active_or_pending, card_not_expired, at_most_n_methods}` (default 5)
- `payment_method.remove.not_last_if_active_subscription`

#### Audit events

- `payment.charged` / `payment.declined` / `payment.errored`
- `payment_method.created` / `removed` / `default_set`
- `payment.attempt_state_drift` (webhook contradicts row)
- `payment.refunded` (record-only on `charge.refunded`)
- `payment.dispute_opened` (record-only on `charge.dispute.created`)

### 7.4 Subscription lifecycle

#### State machine

```
pending → active → blocked → terminated
            ↑↓        ↑↓
         (top_up) (renew)
            ↓
         (exhaust)
```

- `pending` → `active`: on `service_order.completed` event
- `active` → `blocked`: on bundle exhaust (`subscription.exhaust`)
- `blocked` → `active`: on VAS top-up OR renewal-time charge success
- any → `terminated`: voluntary cancel, port-out (with `release_inventory=False`), bereavement, fraud

`is_exhausted` checks the **primary** allowance set only (`data_roaming` is additive, never blocks the subscription state — see [§7.6](#76-roaming-v017)).

#### Data model

| Table | Purpose |
|---|---|
| `subscription.subscription` | `id`, `customer_id`, `offering_id`, `msisdn`, `iccid`, `state`, price snapshot fields, period (`current_period_start/end`, `next_renewal_at`), pending fields (`pending_offering_id`, `pending_offering_price_id`, `pending_effective_at`), renewal worker dedup (`last_renewal_attempted_at`), reminder dedup (`last_renewal_reminder_at`) |
| `subscription.bundle_balance` | `id`, `subscription_id`, `allowance_type`, `total`, `consumed`, `unit`, `period_start/end`. `total = -1` is unlimited |
| `subscription.subscription_state_history` | Every transition with `from`/`to`/`reason`/`changed_by` |
| `subscription.vas_purchase` | `id`, `subscription_id`, `vas_offering_id`, `payment_attempt_id`, `applied_at`, `expires_at`, `allowance_added`, `allowance_type` |

#### Policies

- `subscription.create.{requires_customer, msisdn_and_esim_reserved, requires_active_price, requires_payment_success}`
- `subscription.renew.only_if_active_or_blocked`
- `subscription.transition.invalid` (state-machine)
- `subscription.terminate.invalid_state`
- `subscription.usage_rated.roaming_balance_required` (v0.17)
- `subscription.vas_purchase.{not_if_terminated, vas_offering_sellable, requires_active_cof}`
- Plan-change family — see [§7.9](#79-plan-changes-v07).

#### Operator gotchas

- A `blocked` subscription **does NOT auto-recover** on the next worker tick. The worker emits `subscription.renewal_skipped` for the cockpit dashboard. Customer must top up VAS or update COF; the next period's renewal sweep can pick it up. Manual `bss subscription renew <SUB-id>` is the operator escape hatch.
- `payment.charge` failure during activation triggers a best-effort inventory release (MSISDN + eSIM) before `subscription.create.requires_payment_success` raises. Don't expect the subscription row after a declined activation.
- `terminate(release_inventory=False)` is reserved for port-out (see [§7.7](#77-number-portability--port-in--port-out-v017)); operator-initiated cancels use the default `True`.

### 7.5 Automatic renewal worker (v0.18)

#### What it does

In-process tick loop attached to the subscription service's `lifespan`. Three sweeps per tick:

1. `_sweep_due` — `active` + period elapsed → call `service.renew(sub_id)`.
2. `_sweep_skipped` — `blocked` + overdue → emit `subscription.renewal_skipped` (cockpit dashboard signal; no auto-retry).
3. `_sweep_upcoming_renewal_reminder` — `active` + within `BSS_RENEWAL_REMINDER_LOOKAHEAD_SECONDS` → send reminder via `bss-portal-auth` email adapter.

Multi-replica safe by `FOR UPDATE SKIP LOCKED` + a dedup column committed in the same transaction as the lock release.

#### Correctness invariants (verbatim from the worker module)

1. **Mark-before-dispatch.** SELECT-FOR-UPDATE-SKIP-LOCKED batch commits dedup column write **before** releasing locks. Without this ordering, two replicas can each grab a batch → double billing.
2. **Per-id session for dispatch.** Each `service.renew(sub_id)` runs in its own `async with session_factory()` so one bad row doesn't poison the batch.
3. **ContextVar reset in `finally`.** `auth_context.push(actor="system:renewal_worker", channel="system")` returns a Token; pop in `finally` so values don't leak across iterations.
4. **Cancellation semantics.** Outer loop catches `CancelledError`, logs, re-raises. In-flight `renew()` is NOT shielded — rolls back via its own session; next process picks up because the dedup column was committed.
5. **Wall-clock interval, `bss_clock` WHERE.** `asyncio.sleep(BSS_RENEWAL_TICK_SECONDS)` is real time (operator contract for cadence); WHERE clause uses `bss_clock.now()` so frozen-clock scenarios drive the worker deterministically.

#### Operator escape hatches (not the prod path)

| Surface | How |
|---|---|
| CLI | `bss subscription renew <SUB-id>` (one sub) |
| Cockpit tools | `subscription.renew_now` (one sub), `subscription.tick_renewals_now` (force a sweep) |
| Admin route | `POST /admin-api/v1/renewal/tick-now` (`services/subscription/app/api/renewal_admin.py`) |

#### Operator gotchas

- "Customer says renewal didn't happen" → check `subscription.last_renewal_attempted_at` first.
  - Equals or postdates `next_renewal_at` → worker tried; look for `subscription.renew_failed` event or `renewal.worker.policy_violation` log.
  - `NULL` → worker hasn't ticked since the period boundary (check `BSS_RENEWAL_TICK_SECONDS` and the worker task health).
- Multi-replica deploys: the SKIP LOCKED query divides batches across pods automatically. **Don't add a sibling cron or external scheduler** — that's the v0.18+ doctrine guard.
- Reminders dedup on `last_renewal_reminder_at`; a crashed reminder send won't replay this period (the renewal itself still fires).

### 7.6 Roaming (v0.17)

#### What it does

Roaming is a per-event **attribute** (`roaming_indicator: bool`) on the mediation usage event, **not** a separate event type.

- `VALID_EVENT_TYPES` stays `{data, voice, voice_minutes, sms}`.
- The rating consumer inspects `roamingIndicator` post-rate; if true and the offering carries a `data_roaming` allowance, the `usage.rated` payload routes there.
- If the offering carries no roaming allowance, rating emits `usage.rejected` with `reason="rating.no_roaming_allowance"`.
- On the subscription side, `data_roaming` is **additive**: `is_exhausted` considers only primary allowances. An exhausted roaming balance rejects roaming usage with rule `subscription.usage_rated.roaming_balance_required` while leaving home data working.

#### Surfaces

- **Mediation API:** `POST /tmf-api/usageManagement/v4/usage` accepts `roaming_indicator: bool` (default false).
- **CLI:** `bss usage simulate --roaming` (scenario tool).
- **LLM tools:** `usage.simulate` (operator), `usage.history` / `usage.history_mine` (read).
- **Roaming top-up:** VAS offering with `allowanceType: "data_roaming"` purchased via `vas.purchase_for_me` / `subscription.purchase_vas`. Subscription **materializes** a fresh `BundleBalance` row when no `data_roaming` row exists.

#### Operator gotchas

- A customer whose plan has zero roaming included can still top up via a roaming VAS — the subscription synthesizes the balance row.
- Soak/scenario authors: `roamingIndicator` defaults to false; existing fixtures continue to work unchanged. Routing only triggers when both `roaming_indicator=true` AND offering has `data_roaming` allowance.

### 7.7 Number portability — port-in / port-out (v0.17)

#### What it does

Port requests are **their own aggregate** (`crm.port_request`), not a Case overload. FSM: `requested → validated → completed | rejected`. (`validated` is a hook for a future automated donor-carrier check; v0.17 ships only the operator-driven path so `approve` collapses `requested → completed` directly.)

#### Two flows

**Port-in:** Donor MSISDN inserted into `inventory.msisdn_pool` with `ON CONFLICT DO NOTHING`. If `target_subscription_id` supplied → status `assigned`; otherwise `available` for normal signup.

**Port-out:** Donor MSISDN flips to terminal `ported_out` with `quarantine_until = '9999-12-31'` (so reserve-next predicates can never select it again — port-out is non-recyclable). Then `subscription.terminate(reason="ported_out", release_inventory=False)` so the existing release-msisdn step is skipped (the number now belongs to the recipient carrier). eSIM still recycles. **Order matters: flip first, terminate second**, so a concurrent reserve-next can't grab the number.

#### Surfaces

- **CLI:** `bss port-request list/get/create/approve/reject`
- **Cockpit LLM tools (operator-only):** `port_request.list`, `port_request.get`, `port_request.create`, `port_request.approve`, `port_request.reject`
- **No portal customer surface** (MNP is operator-driven by spec — donor-carrier coordination, fraud screen, regulatory clearance).

#### Operator gotchas

- Port-in conflict (number already in pool) is silent: existing row wins via `ON CONFLICT DO NOTHING`.
- Port-out without `target_subscription_id` raises `port_request.create.target_sub_required_for_port_out`. Order: create with target, **then** approve.
- The `subscription.terminate` call inside port-out approval is wrapped in try/except — failure logs `port_request.subscription_terminate_failed` but doesn't fail the port (the MSISDN is already flipped, which is the regulator-visible state). Operator must re-terminate manually.

For the runbook see [§8.7](#87-mnp--port-in--port-out).

### 7.8 VAS top-ups

Explicit customer-initiated allowance top-up. Charged to the customer's default COF; charge must succeed before the balance updates.

- If subscription is `blocked` → top-up unblocks it (transition `top_up`).
- If `active` → stays active (self-loop).
- VAS offerings are catalogue rows with their own price and allowance spec (`VAS_*`). Renewable bundles (`PLAN_*`) and one-shot top-ups are distinguished by lifecycle + by id prefix convention.

**Surfaces:** `POST /top-up/{vas_offering_id}` (step-up), `bss subscription vas`, `vas.purchase_for_me` (chat), `subscription.purchase_vas` (cockpit).

**Operator gotchas:**
- Zero-allowance VAS leaves balances unchanged — only the `vas_purchase` row exists.
- Unlimited balances (`total = -1`) are not incremented by VAS; the VAS gets a row but the cap stays unlimited.
- `vas.purchase_for_me` is **not** in `DESTRUCTIVE_TOOLS` — chat can purchase without `/confirm`.

### 7.9 Plan changes (v0.7+)

#### What it does

**Pending fields + renewal-time pivot.** Never terminate-and-recreate — that loses VAS top-ups, mis-attributes voluntary churn, leaves the customer without a line.

`subscription.schedule_plan_change` writes:
- `pending_offering_id`
- `pending_offering_price_id`
- `pending_effective_at = next_renewal_at`

The next `renew()` call detects `applying_pending` (effective date arrived) and **atomically**:
1. Charges the new snapshot.
2. Resets balances from the new offering's allowance spec.
3. Zeroes any allowance types the new plan no longer carries.
4. Swaps `offering_id` + price snapshot fields.
5. Clears pending fields.

If the plan-change charge declines: `subscription.plan_change_payment_failed` fires, **and pending fields are intentionally not cleared** so the operator can retry.

#### Operator-initiated price migrations

`migrate_subscriptions_to_price` writes pending fields with the **same** offering id (price-only) and respects a regulator notice window. See [§8.3](#83-migrate-customers-to-a-new-price).

#### Audit events

- `subscription.plan_change_scheduled`
- `subscription.plan_changed` (true plan switch — pending offering ≠ current)
- `subscription.price_migrated` (price-only — pending offering == current)
- `subscription.plan_change_cancelled`
- `subscription.plan_change_payment_failed`
- `subscription.price_migration_scheduled` (one per affected sub) + `notification.requested`

### 7.10 Escalation cases

#### The five non-negotiable categories

`fraud`, `billing_dispute`, `regulator_complaint`, `identity_recovery`, `bereavement`. Plus `other` as a CSR-triaged catch-all.

The chat-side AI has **zero authority** to attempt resolution. On hit, the LLM calls `case.open_for_me`:
1. Hashes the chat transcript SHA-256 and stores via `crm.store_chat_transcript`.
2. Opens a CRM case with `chat_transcript_hash` set as a soft pointer.
3. Maps category → CRM `CaseCategory` (`account` / `billing` / `information`) and priority (`high` for fraud/regulator/identity_recovery; `medium` for billing_dispute/bereavement/other).

Customer sees the verbatim sentence: *"I've opened a case for this. A member of our team will follow up via email at {customer_email}."*

#### Data model

| Table | Purpose |
|---|---|
| `crm.case` | id (`CASE-...`), customer_id, subject, description, state (open/in_progress/pending_customer/resolved/closed), priority, category, opened_by_agent_id, opened_at/closed_at, resolution_code, **chat_transcript_hash** |
| `crm.case_note` | case_id, body, author_agent_id, created_at |
| `crm.ticket` | TMF621 child of Case (1:N) |
| `audit.chat_transcript` | hash + raw transcript text |

#### Policies

- `case.open.customer_must_be_active`
- `case.transition.valid_from_state`
- `case.close.requires_all_tickets_resolved`
- `case.close.requires_resolution_code`

#### Operator gotchas

- The transcript hash on the case is **opaque to the customer** (system prompt instructs the LLM not to surface it). Operators look it up via `case.show_transcript_for`.
- v0.19.1 lesson: **no automated case emails**. Case state changes are operator-visible via cockpit and CSR portal, not customer email (yet).
- Adding a sixth category requires updating three places: the Literal in `types.py`, the system prompt, the soak corpus.

For the runbook see [§8.10](#810-chat--triage-an-escalated-case).

### 7.11 Chat surface (v0.11–v0.12)

Covered in [§6.2](#62-customer-chat-the-only-orchestrator-mediated-route). Key points:

- **The only orchestrator-mediated route in self-serve.**
- **Three-layer containment:** server-side policies + `*.mine` wrappers + output trip-wire.
- Per-customer Conversation store (separate from cockpit's `cockpit.session`).
- `audit.domain_event.service_identity = "portal_self_serve"` for every chat-driven write.
- Cap-trip → templated SSE response, never a raw error.

For runbooks see [§8.8](#88-chat--cap-tripped) (cap-tripped), [§8.9](#89-chat--ownership-trip-p0) (ownership-trip P0), [§8.10](#810-chat--triage-an-escalated-case) (triage), [§8.11](#811-chat--transcript-retention) (retention), [§8.18](#818-adding-a-tool-to-customer_self_serve-security-review) (extending the profile).

### 7.12 Operator cockpit (v0.13)

Covered in [§5.1](#51-the-repl-bss--canonical-cockpit), [§5.2](#52-browser-cockpit-localhost9002), [§5.6](#56-destructive-tool-gating-confirm).

#### System prompt composition

1. `OPERATOR.md` (operator-editable persona + house rules), prepended verbatim, hot-reloaded on mtime.
2. `_COCKPIT_INVARIANTS` (code-defined safety contract — propose-then-confirm, escalation list, ASCII-rendering rules; **not editable**).
3. Per-turn context blocks: `customer_focus` (when pinned via `/focus`), pending-destructive proposal payload (when running with `allow_destructive=True`).

#### Data model

| Table | Purpose |
|---|---|
| `cockpit.session` | id (`SES-YYYYMMDD-<8 hex>`), actor, customer_focus, allow_destructive, state, label, created_at |
| `cockpit.message` | session_id, role, content, tool_name, tool_call_id, raw_result, created_at |
| `cockpit.pending_destructive` | session_id, tool_name, tool_args (JSON), proposed_at; consumed by next `/confirm` |

For the runbook see [§8.14](#814-cockpit-ops-sessions-persona-settings-confirm-forensics).

### 7.13 Tracing & audit

#### What you get

- **OpenTelemetry SDK + auto-instrumentors** (FastAPI, HTTPX, AsyncPG, AioPika) on every service. OTLP/HTTP export to Jaeger.
- **`audit.domain_event`** in every service's schema — written in the **same DB transaction** as the domain write; RabbitMQ publish happens after commit (simplified outbox; the audit log is the durability backstop).
- **`service_identity`** column on `audit.domain_event` (v0.9+) — derived from validated token map, **never** from a forgeable header. Values: `default`, `portal_self_serve`, `operator_cockpit`, `partner_<name>`.
- **`bss trace`** swimlane — ASCII rendering of a Jaeger trace, with `service_identity` per span. Resolves trace IDs from audit events: `bss trace get <trace-id>`, `bss trace for-order <ORD-id>`, `bss trace for-subscription <SUB-id>`.

#### Three columns answer "who did this?"

| Column | What |
|---|---|
| `actor` | Descriptive: `cheekong@…` or `llm-gemma-4-26b` or `system:renewal_worker` |
| `channel` | `cli` / `portal-csr` / `portal-self-serve` / `system` / `scenario` |
| `service_identity` | Validated; one of the named tokens or `default` |

For the BYO Jaeger setup runbook see [§8.15](#815-jaeger-byoi-set-up-tracing-on-a-byo-host).

### 7.14 Cockpit conversation rendering (v0.19)

#### Single source of truth: `bss_cockpit.renderers.dispatch`

Both REPL and browser veneer resolve every `tool`-role row through `render_tool_result(tool_name, raw_json)`:
- **Registered tool** → deterministic ASCII string.
- **Unregistered** → `None`. **Doctrine v0.19+:** the LLM is instructed to surface raw JSON verbatim — never fall back to a markdown table.
- The browser veneer wraps any non-`None` return value in `<pre>` so visible output is byte-identical to the REPL.

#### Currently registered (18 entries)

- Single-entity gets: `subscription.get`, `customer.get` / `customer.find_by_msisdn` (the customer-360 unpacks `_extras` for subscriptions/cases/interactions), `order.get`, `catalog.get_offering`, `inventory.esim.get_activation`, `subscription.get_esim_activation`
- Lists: `subscription.list_for_customer`, `customer.list`, `order.list`, `catalog.list_offerings`, `catalog.list_active_offerings`, `catalog.list_vas`, `inventory.msisdn.list_available`, `inventory.msisdn.count`, `port_request.list`, `port_request.get`
- Balance: `subscription.get_balance`

#### Customer-360 special case

`customer.get` carries an optional `_extras` block (subscriptions / cases / interactions) added by v0.19.1. The dispatcher unpacks it and passes the lists to `render_customer_360`.

#### Operator gotchas

- LLM rendered a markdown table anyway? That's a doctrine bug — file the regression. The system prompt is the contract; if a small model ignores it, the v0.19 lesson says "code-enforce at the surface seam" (the REPL has `_suppress_*` helpers if it ever became necessary). Currently the suppression is prompt-level.
- A tool with no renderer is fine — it just renders raw JSON. **Don't add markdown rendering as a "nicer" intermediate**; doctrine rules out exactly that.

For the snapshot regeneration runbook see [§8.16](#816-snapshot-regeneration-cli-golden-files).

### 7.15 Knowledge tool (v0.20)

#### What it does

The cockpit's `knowledge.search` / `knowledge.get` tools read the
indexed doc corpus (this handbook + CLAUDE.md + runbooks +
ARCHITECTURE.md + DECISIONS.md + TOOL_SURFACE.md + ROADMAP.md +
CONTRIBUTING.md) and return rank-ordered hits. The cockpit's system
prompt instructs the LLM to call `knowledge.search` for any how-to /
what-is / is-this-allowed question outside the deterministic tool
surface, and to cite the returned `anchor` + `source_path` in its
reply.

A **citation guard** (`_RE_KNOWLEDGE_CLAIM` regex at the REPL +
browser cockpit) catches first-person handbook/doctrine claims and
replaces un-cited ones with a templated fallback pointing at `bss
admin knowledge search`. The guard is conservative — it catches the
most common false-confident phrasings ("per the handbook", "according
to doctrine", "the handbook says") and lets nuanced phrasings through
(the search index is the primary defence).

#### Search backends

- `BSS_KNOWLEDGE_BACKEND=fts` (default) — Postgres FTS via
  `tsvector` + GIN index. Zero deps beyond pgvector being installable
  (the column exists; the index pays nothing when empty).
- `BSS_KNOWLEDGE_BACKEND=hybrid` — pgvector cosine similarity over
  embeddings, re-ranked by FTS. Requires `BSS_KNOWLEDGE_EMBEDDER`
  (`openrouter` or `local`).

#### Doctrine: customer chat does NOT get the knowledge tool

The handbook + runbooks describe destructive operator flows, perimeter
posture, KYC bypass flags, the cutover runbook for Stripe. None of
that belongs in customer chat — leaking it to a prompt-injected LLM
would teach it which flag to ask the operator about. The doctrine
guard (`make doctrine-check` rule 15) catches any drift, and
`validate_profiles()` enforces the exclusion at startup.

#### Indexer is operator-initiated

`bss admin knowledge reindex` (or `make knowledge-reindex`). No
file-watcher in the cockpit container — the doc corpus changes with
PRs and reindex runs on demand. Three idempotency layers (mtime cache
→ content_hash dedup → deterministic chunk id) keep re-runs cheap.
`--force` re-upserts all chunks.

For the runbook see [§8.19](#819-knowledge-indexer-reindex--postgres-pgvector-prereq).

---

## Part 8 — Day-2 operations (runbooks)

> Each runbook is self-contained. Cross-reference [Part 7](#part-7--domain-features) for domain context.

### 8.1 Catalog — add an offering (with roaming)

**Audience:** operators with admin access running BSS-CLI v0.7+. v0.17 introduced roaming as a first-class allowance type but the CLI flag set hasn't caught up — see [Roaming gap](#81a-roaming-gap-and-workaround).

#### Prerequisites

- `bss` CLI installed and pointed at target deployment (`BSS_API_TOKEN`, service URLs in `.env`).
- A `--spec-id` value already in `catalog.product_specification`. The seeded `SPEC_MOBILE_PREPAID` covers v0.7+ needs.

#### Procedure (data + voice + SMS, no roaming)

```bash
bss admin catalog add-offering \
    --id PLAN_XS \
    --name "Mini" \
    --price 5.00 \
    --currency SGD \
    --data-mb 5120
```

The command writes one `product_offering` row, one `product_offering_price` row (`PRICE_PLAN_XS`), and one `bundle_allowance` row through the catalog admin service. No raw SQL.

Optional flags: `--voice-min 100`, `--sms-count 100`, `--valid-from <iso>`, `--valid-to <iso>`.

#### Verify

```bash
bss admin catalog show          # Active catalog at the current moment
bss catalog list                # Customer-facing read
```

#### 8.1a Roaming gap and workaround

> [!warning] **CLI gap (acknowledged):** `bss admin catalog add-offering` does **not** yet expose a `--data-roaming-mb` flag. Server-side support exists (the catalog service writes `data_roaming` rows from seed; subscription materializes balances on top-up), but the admin HTTP endpoint and CLI haven't been extended.
>
> **Until that flag lands, two paths:**

**Path A — Add the offering, then attach a `data_roaming` allowance row directly via SQL:**

```bash
# 1. Create the offering with primary allowances.
bss admin catalog add-offering \
    --id PLAN_XS_ROAM \
    --name "Mini + Roaming" \
    --price 8.00 \
    --data-mb 5120

# 2. Attach the roaming allowance.
psql "$BSS_DB_URL" <<SQL
INSERT INTO catalog.bundle_allowance (id, offering_id, allowance_type, quantity, unit)
VALUES ('BA_PLAN_XS_ROAM_ROAM', 'PLAN_XS_ROAM', 'data_roaming', 1024, 'mb');
SQL

# 3. Verify (the offering now lists 4 allowance lines: data, data_roaming, voice if any, sms if any).
bss admin catalog show
```

**Path B — Extend the seed module and re-seed (recommended for permanent additions):**

Edit `packages/bss-seed/bss_seed/catalog.py` (the `allowances` list around line 62 carries the seeded plans + their `data_roaming` rows in the standard 4-line pattern). `make seed` is idempotent (`ON CONFLICT DO NOTHING`); rows already in DB are untouched, new rows land.

#### Customers without included roaming can still top up

A customer on `PLAN_S` (which carries `data_roaming = 0 mb`) **can still purchase `VAS_ROAMING_1GB`**. The subscription's `purchase_vas` materializes a `data_roaming` `BundleBalance` row on demand. After exhaustion, roaming usage is rejected with `subscription.usage_rated.roaming_balance_required` while home data keeps working — see [§7.6](#76-roaming-v017).

#### Time-window an offering at creation

```bash
bss admin catalog add-offering \
    --id PLAN_CNY \
    --name "Lunar New Year Promo" \
    --price 12.00 \
    --valid-from 2026-02-10T00:00:00Z \
    --valid-to 2026-02-24T00:00:00Z \
    --data-mb 30720
```

After 2026-02-24, `bss catalog list` no longer surfaces `PLAN_CNY` for new orders. Customers who ordered during the window keep their snapshot price untouched.

#### Rollback

```bash
bss admin catalog window-offering --id PLAN_XS --valid-to <now>
```

Retires the offering immediately for new orders. Existing subscriptions keep renewing on their snapshot price; to migrate them off see [§8.3](#83-migrate-customers-to-a-new-price).

### 8.2 Catalog — run a promo

Two patterns:

#### Pattern A — windowed offering

A new product with its own SKU, only sellable in a date range. Use [§8.1](#81-catalog--add-an-offering-with-roaming) with `--valid-from` / `--valid-to`. After the window, the SKU is invisible to new orders; existing subscribers keep their snapshot.

#### Pattern B — windowed price on an existing offering

A temporary discount on an existing plan. **The plan keeps its base price**; you add a second `product_offering_price` row valid only inside the window:

```bash
bss admin catalog set-price \
    --offering PLAN_M \
    --amount 29.00 \
    --currency SGD \
    --valid-from 2026-02-10T00:00:00Z \
    --valid-to 2026-02-24T00:00:00Z \
    --price-id PRICE_PLAN_M_CNY
```

`catalog.list_active_offerings` returns the lower of overlapping prices for `valid_from <= now < valid_to`. New orders during the window snapshot the discounted price; existing subscribers are unaffected (motto: snapshot pricing).

**Verify:**

```bash
bss admin catalog show --at 2026-02-15T00:00:00Z
```

**Anti-pattern:** UPDATE-ing the existing price row to the discount and reverting later. That breaks the snapshot for any subscriber who ordered between your update and revert.

#### Pattern C — promo codes (v1.1)

A third mechanism, distinct from A/B: **loyalty-cli entitlements**, for typed codes and targeted offers. The discount *composes* on top of the lowest-active price (it stacks, unlike windowed prices).

```bash
# Non-targeted typed code (customer enters SUMMER25 at checkout):
bss promo create --id PROMO_SUMMER25 --type percent --value 20 \
    --duration multi --periods 3 --code SUMMER25 --code-kind multi_use

# Targeted code (in loyalty but unadvertised; auto-applies for eligible customers):
bss promo create --id PROMO_VIP --type percent --value 20 --duration single --audience targeted
bss promo assign --promo PROMO_VIP --customers CUST-001,CUST-007   # eligibility list
```

Consumed at activation (a provisioning failure never burns a code; a payment decline revokes it). Renewal decrements a per-subscription counter; a plan change ends the promo. Operator-only — customers type a code, never issue one. **loyalty-cli is an optional adapter** — with `BSS_LOYALTY_API_TOKEN` unset, catalog/COM/CRM still boot and everything works minus promotions (orders proceed at full price). **Full runbook: [`docs/runbooks/promo-codes.md`](runbooks/promo-codes.md)** (env, optional behavior, duration semantics, lifecycle, troubleshooting).

### 8.3 Migrate customers to a new price

> [!info] **Why this is its own flow.** Catalog price changes by design DO NOT affect existing subscribers (snapshot pricing, motto #7). To move existing subscribers, an explicit operator-initiated migration with regulator notice is required.

#### Procedure

```bash
# 1. Add the new price (windowed or not).
bss admin catalog set-price \
    --offering PLAN_M \
    --amount 45.00 \
    --currency SGD \
    --valid-from 2026-04-01T00:00:00Z \
    --price-id PRICE_PLAN_M_2026Q2 \
    --retire-current        # stamps valid_to on the prior PRICE_PLAN_M

# 2. Schedule the migration. Pending fields written with same offering_id;
#    next renewal pivots; notification.requested event emitted per sub.
bss admin catalog migrate-price \
    --offering PLAN_M \
    --new-price-id PRICE_PLAN_M_2026Q2 \
    --effective-from 2026-04-01T00:00:00Z \
    --notice-days 30 \
    --initiated-by ops
```

#### What happens on the next renewal

Each sub's `renew()` detects pending fields, charges the new price, snapshots it, clears pending. Audit event `subscription.price_migrated`. Subs terminated during the notice window simply skip the pivot.

#### Notification

`notification.requested` events fire one per sub when migration is scheduled. v0.14+ the Resend adapter sends the email if configured; otherwise it lands in `LoggingEmailAdapter` for demo. **Regulator note (Singapore IMDA):** ≥30-day notice for material price changes is standard.

#### Anti-pattern

Schedule a plan-change as `terminate-and-recreate`. Loses VAS top-ups, mis-attributes voluntary churn, leaves the customer without a line.

### 8.4 Rotate API tokens

> [!info] **Token model:**
> - **v0.3:** single `BSS_API_TOKEN` gates every BSS service.
> - **v0.9:** named tokens per external-facing surface (`BSS_PORTAL_SELF_SERVE_API_TOKEN`, `BSS_OPERATOR_COCKPIT_API_TOKEN`, `BSS_PARTNER_<NAME>_API_TOKEN`). Identity resolved from validated `TokenMap` lookup; receiving services derive `service_identity` from it. Default-fallback identity is `default`.

#### When to rotate

- Annual baseline.
- Suspected leak (pastebin scrape, lost laptop, ex-employee).
- Provider compromise.
- Compliance requirement.

#### Procedure (any token; restart-based)

```bash
# 1. Generate the new token.
NEW=$(openssl rand -hex 32)
echo "New token: $NEW"

# 2. Edit .env: replace the value.
# 3. Restart the affected container (one for cockpit; many for BSS_API_TOKEN).
docker compose down && docker compose up -d
```

The new token is loaded at lifespan boot; old tokens become invalid immediately (the `TokenMap` is replaced atomically).

#### Rotating `BSS_API_TOKEN` (the default identity)

This affects orchestrator, REPL, scenarios, all default outbound. After rotating: `make scenarios-hero` to confirm; check `bss external-calls --since 5m` for any 401s on background jobs.

#### Rotating `BSS_PORTAL_SELF_SERVE_API_TOKEN`

Affects only the self-serve portal's outbound identity. **Other surfaces keep working.** This is the v0.9 blast-radius reduction.

#### Rotating `BSS_OPERATOR_COCKPIT_API_TOKEN`

Affects only the cockpit's outbound identity. Same procedure.

#### Detecting a leaked named token

```sql
-- 1. Recent writes per service_identity (anomaly detection).
SELECT service_identity, count(*), min(occurred_at), max(occurred_at)
FROM audit.domain_event
WHERE occurred_at > now() - interval '24 hours'
GROUP BY service_identity
ORDER BY count(*) DESC;

-- 2. Specific identity's recent writes.
SELECT * FROM audit.domain_event
WHERE service_identity = 'partner_acme'
  AND occurred_at > now() - interval '24 hours'
ORDER BY occurred_at DESC LIMIT 100;

-- 3. Outbound provider calls attributed to this identity.
SELECT * FROM integrations.external_call
WHERE service_identity = 'partner_acme'
  AND created_at > now() - interval '24 hours';
```

#### Adding a new named token

1. Add `BSS_PARTNER_ACME_API_TOKEN=<openssl rand -hex 32>` to `.env`.
2. Restart affected services.
3. Configure the partner client to send `X-BSS-API-Token: <value>`.
4. The receiving service stamps `service_identity="partner_acme"` on every audit row from this caller.

> [!warning] **Don't share a named token across surfaces.** Each external-facing surface gets its own `BSS_<NAME>_API_TOKEN`. Sharing defeats the blast-radius reduction.

### 8.5 Stripe cutover (mock → production)

#### When this matters

You're flipping `BSS_PAYMENT_PROVIDER=mock → stripe` (or `stripe → stripe` with a key rotation that affects customer-attached methods). Every existing customer's `payment_method.token` becomes unusable when the active tokenizer changes.

#### Two paths

**Path 1 — Lazy-fail (default; honest with bundled-prepaid posture)**

The `payment.charge.token_provider_matches_active` policy raises on every charge against a non-matching-provider token. Customer sees "your saved payment method is no longer valid" and re-adds via Stripe Checkout.

**Honest, but creates a wave of failed renewals on the next renewal boundary.** Right path for sandbox / demo / small dev.

**Path 2 — Proactive (recommended for production with active subscribers)**

```bash
# 1. (≥7 days before flip) Run a dry run to confirm scope.
bss payment cutover --invalidate-mock-tokens --dry-run

# 2. Send customer-comms (email all affected customers — see comms checklist below).

# 3. (Day of flip) Mass-invalidate.
bss payment cutover --invalidate-mock-tokens

# 4. Flip the env var.
sed -i 's/^BSS_PAYMENT_PROVIDER=mock$/BSS_PAYMENT_PROVIDER=stripe/' .env

# 5. Set BSS_PAYMENT_STRIPE_API_KEY, BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY,
#    BSS_PAYMENT_STRIPE_WEBHOOK_SECRET in .env (see Part 4.4).

# 6. Restart payment + portal containers.
docker compose down payment portal-self-serve
docker compose up -d payment portal-self-serve
```

The cutover command marks every active mock-token row `status=expired`; emits `payment_method.cutover_invalidated` per row.

#### Customer-comms checklist (Path 2)

- Send 7 days before flip: "We're updating our payment provider. Re-add your card by [date] to avoid service interruption."
- Send 24 hours before flip: same message, urgent.
- Send at flip: "We've updated our payment provider. Add a card at <portal>/payment-methods/add to keep service active."

#### What can go wrong

| Symptom | Cause | Fix |
|---|---|---|
| Cutover during renewal window | Half the renewal batch goes to mock (silent success) and half to Stripe (fails on saved cards) | Cut over OUTSIDE renewal windows. Use `bss-clock` to verify no jobs in next hour |
| Wrong `whsec_*` | Charges work but webhooks 401 | Diagnostic structlog (`payment.webhook.signature_invalid` with `candidate_headers` + `body_preview`) makes this obvious in minutes. Rotate secret then `stripe events resend` from dashboard |
| `BSS_ENV` not set to `production` | `sk_test_*` allowed; safety belts off | Set `BSS_ENV=production` and restart |
| Customer races: re-adds card before flip | Mock-mode add — token invalidated on flip | Path 2's mass-invalidate handles. Customer re-adds again post-flip |

#### The trap

> [!warning] **Don't switch payment providers without a documented cutover.** A `payment_method.token` minted as a mock token is unusable when Stripe is selected. Either run cutover proactively or accept lazy-fail with documented customer-comms; never flip the env var blind.

### 8.6 Three-provider sandbox soak (release gate)

#### What this is for

Pre-tag release-gate soak for the three real-provider integrations (Stripe + Resend + Didit). **Three consecutive green runs = release confidence.** Lives at `scenarios/live_sandbox/`.

#### What runs (5 tiers)

1. **Tier 1 — Credentials valid.** `bss onboard --domain {email|kyc|payment} --probe`-style smoke; no test data created.
2. **Tier 2 — Sandbox round-trip.** Real provider call against sandbox creds; verify response shape + recorded `integrations.external_call` row.
3. **Tier 3 — BSS adapter live.** Adapter exercise against sandbox: `EmailAdapter.send_login`, `KycAdapter.initiate`, `TokenizerAdapter.charge`.
4. **Tier 4 — Env presence.** Resend + Didit creds present (no mocked fallback); fails loud if missing.
5. **Tier 5 — Webhook round-trip.** Provider → public URL (Tailscale Funnel / ngrok / prod) → BSS receiver → reconciliation. Gated on `BSS_NIGHTLY_SANDBOX=true` and `BSS_PAYMENT_WEBHOOK_PUBLIC_URL`.

#### Running the soak

```bash
# Set creds for all three providers.
export BSS_NIGHTLY_SANDBOX=true
# Stripe (sandbox-only):
export BSS_PAYMENT_STRIPE_API_KEY=sk_test_...
export BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY=pk_test_...
export BSS_PAYMENT_STRIPE_WEBHOOK_SECRET=whsec_...
# Didit (sandbox):
export BSS_PORTAL_KYC_DIDIT_API_KEY=...
export BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID=...
export BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET=...
# Resend (live key OK; send-only scope recommended):
export BSS_PORTAL_EMAIL_RESEND_API_KEY=re_...
export BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET=whsec_...
export BSS_PORTAL_EMAIL_FROM='BSS-CLI <noreply@yourdomain.com>'
# Tier 5 webhook URL (public):
export BSS_PAYMENT_WEBHOOK_PUBLIC_URL=https://your-ngrok.io

# Run the soak.
make scenarios-soak
```

#### Sanity guards

- **Refuses `sk_live_*`** — non-negotiable safety belt.
- Bails if any of the named tokens are missing in Tier 4.
- Tier 5 prints the public URL it expects to round-trip with — read it, set up your tunnel.

#### Reading the output

Each tier prints a green/red one-liner. Three full-green runs in a row = ship. Single red = investigate; usually webhook-secret mismatch or upstream sandbox flake.

#### Manual smoke before release

After three green soaks, manually:
1. Sign up a customer end-to-end against Didit sandbox.
2. Verify magic-link delivers via Resend.
3. Add a card via Stripe Checkout sandbox.
4. Place an order; verify activation event chain.
5. Trigger a roaming usage event with `--roaming`.
6. Open and approve a port-in.
7. Schedule and pivot a plan change.

### 8.7 MNP — port-in / port-out

#### Surfaces

- **REPL slash:** `/ports list`, `/ports approve PORT-NNN`, `/ports reject PORT-NNN <reason>`
- **CLI:** `bss port-request list/get/create/approve/reject`
- **Cockpit LLM tools:** `port_request.{list,get,create,approve,reject}` (operator-only — not in `customer_self_serve`)

#### Port-in flow

```bash
# 1. Receive donor-side coordination details (donor carrier, donor MSISDN, port date).
# 2. Optional fraud-screen + regulatory clearance off-system.

# 3. Create the port request (target_subscription_id is optional for port-in).
bss port-request create \
    --direction port_in \
    --donor-carrier "DonorCo" \
    --donor-msisdn "+6591234567" \
    --requested-port-date 2026-05-15

# 4. On the agreed port date, approve it.
bss port-request approve PORT-001
```

What happens: MSISDN inserted into `inventory.msisdn_pool` with `ON CONFLICT DO NOTHING`. Status `available` (or `assigned` if `target_subscription_id` was provided). Audit: `port_request.created`, `port_request.completed`, `inventory.msisdn.seeded_from_port_in`.

#### Port-out flow

```bash
# 1. Recipient carrier requests port-out for an existing customer's MSISDN.
bss port-request create \
    --direction port_out \
    --donor-carrier "RecipientCo" \
    --donor-msisdn "+6598765432" \
    --target-subscription-id SUB-042 \
    --requested-port-date 2026-05-20

# 2. On port date, approve.
bss port-request approve PORT-002
```

What happens (in order):
1. MSISDN flips to terminal `ported_out` with `quarantine_until = '9999-12-31'`.
2. `subscription.terminate(reason="ported_out", release_inventory=False)` is called — eSIM recycles, MSISDN does NOT (it's gone to recipient).

Audit: `port_request.completed`, `inventory.msisdn.ported_out`, `subscription.terminated`.

#### Rejecting a request

```bash
bss port-request reject PORT-003 --reason "donor MSISDN not found on our network"
```

Required: a reason string (policy `port_request.reject.requires_reason`).

#### Doctrine reminders

- **Port-out is non-recyclable.** Status `ported_out` + `quarantine_until = '9999-12-31'` ensures the reserve-next predicate (`status='available'`) never selects it again.
- **Order of operations matters in port-out.** Flip first, terminate second — otherwise a concurrent reserve-next can grab the number.
- **MNP is operator-driven by spec.** No customer self-serve port-out — donor-carrier coordination, fraud-screen, regulatory clearance.

#### Limits (intentional)

- No automated donor-carrier check (`validated` state is a future hook).
- No batch port-in (one PORT request per number).

### 8.8 Chat — cap tripped

#### Symptom

Customer reports: "I hit my chat cap but I haven't been chatting much."

#### Two caps

| Cap | Default | Storage |
|---|---|---|
| Hourly rate per customer | 20 turns | In-memory sliding window (`bss_orchestrator.chat_caps`) |
| Monthly cost per customer | 200 cents (SGD) | DB: `audit.chat_usage` |

Customer also has a per-IP rate cap (60/hour, in-memory).

#### Confirm the count is correct

```sql
-- Monthly cost-to-date for a customer.
SELECT customer_id, period_start, total_cost_cents, total_input_tokens, total_output_tokens
FROM audit.chat_usage
WHERE customer_id = 'CUST-001'
  AND period_start >= date_trunc('month', now());
```

#### Reset (operator-initiated)

The hourly rate window is in-memory; restarting the portal container clears it. The monthly cost cap is a DB row; if the customer's count is genuinely wrong (e.g. a doctrine bug double-counted), update with care:

```sql
-- Audit before any update.
INSERT INTO crm.case_note (case_id, body, author_agent_id, created_at)
VALUES ('CASE-099', 'Resetting chat_usage for CUST-001: <reason>', 'ops', now());

-- Then update.
UPDATE audit.chat_usage
SET total_cost_cents = 0, total_input_tokens = 0, total_output_tokens = 0
WHERE customer_id = 'CUST-001' AND period_start = '2026-05-01';
```

#### Hourly window troubleshoot

If the customer says they've barely chatted but hit the hourly cap, suspect an automated client (browser extension, custom script). Check `audit.chat_usage` for the per-day count.

#### What NOT to do

- Don't raise the global cap to "fix" one customer — bump the per-customer cap if you must, but caps are a deliberate fraud guard.
- Don't delete `audit.chat_usage` rows — that loses the forensic trail. UPDATE with a case note.

### 8.9 Chat — ownership-trip (P0)

#### Symptom

Output trip-wire (`AgentOwnershipViolation`) fires because a chat-surface tool returned data not belonging to the bound customer. Customer sees a generic safety reply (no leaked tool name).

This is **P0** — possible cross-customer data leak.

#### Triage queries

```sql
-- 1. The violation row.
SELECT *
FROM audit.domain_event
WHERE event_type = 'agent.ownership_violation'
  AND occurred_at > now() - interval '24 hours'
ORDER BY occurred_at DESC;
```

The structlog event `agent.ownership_violation` carries: `actor` (bound customer id), `tool_name`, `expected`, `got` (the offending response field).

#### Common causes

1. **A wrapper missed an alias.** e.g. `subscription.list_mine` returned a response with a customer-id key the trip-wire's `OWNERSHIP_PATHS` entry doesn't list. Fix: add the alias to `OWNERSHIP_PATHS`.
2. **A canonical tool started returning more than its contract advertises.** e.g. `subscription.list_for_customer` was extended to include a sibling customer's data via a join. Fix: revert the canonical change OR narrow the response.
3. **A new tool was added to `customer_self_serve` profile without `OWNERSHIP_PATHS`.** Fix: add the entry; `validate_profiles()` should have caught this at startup — investigate why it didn't.

#### Remediation order

1. Confirm the violation row + the customer's identity.
2. Check whether the customer actually saw cross-customer data (the response is logged in `agent.ownership_violation.got`).
3. **If yes:** open an incident; notify per data-leak policy; rotate the affected customer's session; consider a regulator notification.
4. Patch the wrapper / `OWNERSHIP_PATHS` / canonical tool.
5. Add a regression test.

#### What NOT to do

- Don't extend `OWNERSHIP_PATHS` without understanding the leak.
- Don't tell the customer the trip-wire fired — the doctrine reply is intentionally opaque (no leaked tool name).

### 8.10 Chat — triage an escalated case

CSR triage of cases auto-opened by the chat surface via `case.open_for_me` (with `chat_transcript_hash`).

#### Identify chat-escalated cases

```sql
SELECT id, customer_id, category, priority, opened_at, chat_transcript_hash
FROM crm.case
WHERE chat_transcript_hash IS NOT NULL
  AND state IN ('open', 'in_progress')
ORDER BY opened_at DESC;
```

#### Open the case in cockpit

```
/360 CUST-NNN
case.show_transcript_for CASE-NNN     # Renders the chat transcript
```

OR in browser cockpit: navigate to `/case/{case_id}` — case-detail page renders notes, tickets, and the chat transcript panel inline.

#### Triage decisions per category

| Category | Priority | Standard playbook |
|---|---|---|
| `fraud` | high | Suspend COF; freeze pending orders; coordinate with payment provider; legal hold |
| `billing_dispute` | medium | Confirm `payment_attempt` ↔ usage trail; refund via dashboard if warranted; record `case.add_note` with reasoning |
| `regulator_complaint` | high | Acknowledge per IMDA timelines; coordinate with compliance; document chain of custody |
| `identity_recovery` | high | Re-verify identity (separate channel); rotate session; consider step-up KYC |
| `bereavement` | medium | Verify documents off-system; transfer or terminate per family request |
| `other` | medium | Free-form triage |

#### Record the resolution

```bash
bss case add-note CASE-042 --body "Refund processed; webhook reconciled; closed."
bss case close CASE-042 --resolution-code resolved
```

`case.close.requires_resolution_code` policy enforces. `case.close.requires_all_tickets_resolved` policy enforces.

#### Adversarial transcripts

The transcript hash is on the case row but the AI cannot surface it to the customer (system prompt rule). If the transcript shows attempted prompt-injection / cross-customer probing, file a soak corpus regression and consider tightening the chat system prompt.

### 8.11 Chat — transcript retention

#### Retention rule

`audit.chat_transcript` rows attached to **closed** cases > 90 days old are eligible for archival.

#### Archive job (manual; v0.12)

```sql
-- Identify candidates.
SELECT t.hash, c.id, c.customer_id, c.closed_at
FROM audit.chat_transcript t
JOIN crm.case c ON c.chat_transcript_hash = t.hash
WHERE c.state = 'closed'
  AND c.closed_at < now() - interval '90 days'
LIMIT 100;

-- Export to cold storage of your choice (S3, etc.) BEFORE deleting.

-- Then delete.
DELETE FROM audit.chat_transcript
WHERE hash IN (SELECT chat_transcript_hash FROM crm.case
               WHERE state='closed' AND closed_at < now() - interval '90 days'
               AND chat_transcript_hash IS NOT NULL)
LIMIT 100;
```

The `chat_transcript_hash` on the case row is opaque; deleting the transcript leaves the hash but `case.show_transcript_for` returns "transcript archived."

#### When to tighten

If regulator pressure or storage cost forces tightening, consider:
- 30-day retention on `low`-priority closed cases.
- Indefinite retention on `fraud` / `regulator_complaint` cases (legal hold).

#### What NOT to do

- Don't delete `crm.case` rows. The case row is the audit anchor; the transcript is the supplemental detail.
- Don't archive transcripts for **open** or **resolved** cases — they may still be needed.

### 8.12 Post-login self-serve diagnostics

Diagnostics for the v0.10+ authenticated post-login portal routes (plan change, payment methods, eSIM, cancel, profile, billing history).

#### Diagnosing a stuck plan change

```sql
SELECT id, state, offering_id, pending_offering_id, pending_offering_price_id,
       pending_effective_at, next_renewal_at
FROM subscription.subscription
WHERE id = 'SUB-NNN';
```

If `pending_effective_at <= now()` and the plan hasn't pivoted, the renewal worker hasn't ticked since (check `BSS_RENEWAL_TICK_SECONDS` / worker log) OR the renewal charge declined (`subscription.plan_change_payment_failed` event).

#### Customer can't add a card

| Symptom | Cause | Fix |
|---|---|---|
| `payment_method.add.at_most_n_methods` | Already has 5 methods | Customer must remove one first |
| `payment_method.add.card_not_expired` | Card already expired at submit | Customer re-enters fresh card |
| Mock-mode: `tok_FAIL_*` returned by tokenizer | Test affordance for failure | Use a non-FAIL token in dev |
| Stripe: `payment_method_already_attached` to a **different** customer | Sandbox shared `pm_card_visa` | Set `BSS_PAYMENT_ALLOW_TEST_CARD_REUSE=true` (sandbox only) |
| Stripe: `card_declined` at attach | Issuer rejected the attach probe | Customer uses different card |

#### Customer claims they didn't authorize X

```sql
-- Pull the portal-action audit row.
SELECT * FROM portal_auth.portal_action
WHERE customer_id = 'CUST-001'
  AND label IN ('vas_purchase', 'plan_change_schedule', ...)
ORDER BY created_at DESC LIMIT 50;
```

The `portal_action` row carries: `label`, `outcome`, `step_up_token_id` (proves a verified OTP was consumed), IP, user agent. Cross-reference with `audit.domain_event`.

#### Email-change stuck pending

```sql
SELECT * FROM portal_auth.email_change_pending
WHERE customer_id = 'CUST-001';
```

If `expires_at < now()`, the customer must restart. The cross-schema email-change flow is the only documented exception to "one route → one write" — see [§7.2](#72-customer-signup-with-kyc) (the same pattern).

### 8.13 Portal auth ops (pepper, identities, brute force)

#### Generating the token pepper

```bash
echo "BSS_PORTAL_TOKEN_PEPPER=$(openssl rand -hex 32)" >> .env
```

Required at portal startup. Sentinel `"changeme"` is rejected.

#### Rotating the pepper

> [!warning] **Pepper rotation invalidates every outstanding magic-link / OTP / step-up token.** Every active customer login must redo email verification.

Rotation is restart-based:
1. Generate new pepper.
2. Update `.env`.
3. Restart portal: `docker compose restart portal-self-serve`.

#### Cleaning up unverified identities

```sql
-- Identities created > 30 days ago that never linked to a customer.
SELECT id, email, created_at
FROM portal_auth.identity
WHERE customer_id IS NULL
  AND created_at < now() - interval '30 days';
```

Safe to delete (no customer dependency).

#### Investigating brute force

```sql
-- Recent verify failures by IP.
SELECT ip, count(*) AS attempts, max(created_at)
FROM portal_auth.login_attempt
WHERE outcome = 'verify_failed'
  AND created_at > now() - interval '1 hour'
GROUP BY ip
HAVING count(*) > 10
ORDER BY attempts DESC;
```

If you see a hot IP, the per-IP rate cap (`BSS_PORTAL_LOGIN_PER_IP_MAX` / `_WINDOW_S`) should be limiting them. Consider reducing the window or adding the IP to upstream firewall.

#### Local dev mailbox

In dev:

```bash
tail -f .dev-mailbox/portal-mailbox.log
```

`LoggingEmailAdapter` writes OTPs + magic links here when `BSS_PORTAL_EMAIL_PROVIDER=logging`.

#### Forcing session revocation

```sql
-- Invalidate one customer's sessions.
DELETE FROM portal_auth.session WHERE customer_id = 'CUST-001';
```

Customer is logged out everywhere; their next request 401s and redirects to login.

### 8.14 Cockpit ops (sessions, persona, settings, /confirm forensics)

#### Resuming a session across surfaces

REPL → browser:

```bash
bss --list                              # Print recent sessions
# Note the SES-... id; open in browser:
open http://localhost:9002/cockpit/SES-20260504-abcd1234
```

Browser → REPL:

```bash
bss --session SES-20260504-abcd1234     # Resume specific session
```

#### Listing sessions in REPL

```
/sessions
```

Renders a Rich table of recent sessions with id, label, last activity, customer focus.

#### Editing `OPERATOR.md` / `settings.toml`

In REPL:

```
/operator edit                          # Opens .bss-cli/OPERATOR.md in $EDITOR
/config edit                            # Opens .bss-cli/settings.toml in $EDITOR
```

Both hot-reload on mtime; no restart needed. Editing `_COCKPIT_INVARIANTS` (in `prompts.py`) requires a code change + restart.

#### Audit attribution after operator change

`actor` is hardcoded `"operator"` since v0.13.1. Forensic per-model attribution lives separately in `audit.domain_event.actor` for LLM-driven calls (e.g. `llm-gemma-4-26b-a4b-it`).

If a deployment needs multi-operator separation, the path is **multi-tenant carve-out** (one cockpit container per operator namespace), not a login wall.

#### Investigating a cockpit destructive action (4-table paper trail)

When a destructive action ran, four tables tell the story:

```sql
-- 1. The proposal (cockpit.pending_destructive — usually consumed by /confirm).
-- (Row is deleted on consumption; kept only when /confirm hasn't fired yet.)

-- 2. The /confirm message (cockpit.message; role='user', content='/confirm').
SELECT * FROM cockpit.message
WHERE session_id = 'SES-20260504-abcd1234'
  AND content = '/confirm'
ORDER BY created_at DESC LIMIT 1;

-- 3. The tool call (cockpit.message; role='tool', tool_name=<destructive>).
SELECT * FROM cockpit.message
WHERE session_id = 'SES-20260504-abcd1234'
  AND tool_name IN ('subscription.terminate', 'customer.close', 'order.cancel', ...)
ORDER BY created_at DESC LIMIT 5;

-- 4. The audit row (audit.domain_event with the resulting state-change).
SELECT * FROM audit.domain_event
WHERE actor LIKE 'llm-%'
  AND service_identity = 'operator_cockpit'
  AND event_type IN ('subscription.terminated', 'customer.closed', 'order.cancelled')
ORDER BY occurred_at DESC LIMIT 5;
```

#### Rotating `BSS_OPERATOR_COCKPIT_API_TOKEN`

Same as [§8.4](#84-rotate-api-tokens). Restart-based. Cockpit-only; doesn't affect other surfaces.

### 8.15 Jaeger BYOI (set up tracing on a BYO host)

Skip if you're using `make up-all` (bundled mode).

#### Recommended (compose)

On the BYOI host:

```yaml
# docker-compose.tracing.yml
version: "3.8"
services:
  jaeger:
    image: jaegertracing/all-in-one:1.65.0
    ports:
      - "4317:4317"     # OTLP gRPC
      - "4318:4318"     # OTLP HTTP
      - "16686:16686"   # UI
    environment:
      COLLECTOR_OTLP_ENABLED: "true"
```

```bash
docker compose -f docker-compose.tracing.yml up -d
```

#### Alternative (docker run)

```bash
docker run -d --name jaeger \
  -p 4317:4317 -p 4318:4318 -p 16686:16686 \
  -e COLLECTOR_OTLP_ENABLED=true \
  jaegertracing/all-in-one:1.65.0
```

#### Configure BSS to export

In `.env`:

```bash
BSS_OTEL_ENABLED=true
BSS_OTEL_EXPORTER_OTLP_ENDPOINT=http://<your-tracing-host>:4318
BSS_OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
BSS_OTEL_SAMPLING_RATIO=1.0
```

Restart all BSS service containers.

#### Verify

Trigger any operation:

```bash
bss customer create --email test@example.com --name "Test User"
```

Then in Jaeger UI (`http://<your-tracing-host>:16686`), search service `bss-crm`. You should see one trace covering the create call.

Alternatively from the CLI:

```bash
bss trace for-ask "$(bss customer list --limit 1 --json | jq -r '.[0].id')"
```

#### Storage trade-off

The `all-in-one` image uses in-memory storage by default; traces are lost on restart. For longer retention, switch to `--storage backend=badger` or use a separate Cassandra/ES backend.

#### Troubleshooting (spans not appearing)

1. **`BSS_OTEL_ENABLED=false`?** Default is `true`; verify env.
2. **Wrong endpoint protocol?** `http/protobuf` for port 4318; `grpc` for 4317.
3. **Service can't reach Jaeger?** From container: `curl -v http://jaeger:4318/v1/traces` should return 405 Method Not Allowed (POST-only) — that's healthy.
4. **Sampling = 0?** `BSS_OTEL_SAMPLING_RATIO=1.0` for dev.
5. **Service started before Jaeger?** OTel SDK retries with backoff; check structlog for `otlp.export_failed`.
6. **Firewall?** Ports 4317/4318 must be open between BSS host and Jaeger host.
7. **Port 16686 not exposed?** UI can't render even if collector is fine.

### 8.16 Snapshot regeneration (CLI golden files)

#### When to regenerate

Renderer output changed deliberately (e.g. you added a column, changed truncation, added an `_extras` block). NOT when a test failure surprises you — that's a regression.

#### How to regenerate

```bash
UPDATE_SNAPSHOTS=1 uv run pytest cli/tests/      # All CLI snapshots
UPDATE_SNAPSHOTS=1 uv run pytest cli/tests/v0_19/snapshots/   # One renderer's snapshots
```

The snapshot fixture re-records `.snap` files. Review the diff carefully:

```bash
git diff cli/tests/snapshots/
```

Commit only when the change is the intended visual update.

#### Common pitfalls

- Renderer relies on `bss_clock.now()` instead of `datetime.utcnow()` — the clock-frozen test fixture pins time so snapshots are deterministic. New `datetime.utcnow()` calls break snapshots non-deterministically.
- Adding a column to a list renderer changes width of every other row — review all snapshots for that renderer, not just the new one.

### 8.17 Payment idempotency forensics

#### v0.16 contract

`idempotency_key = f"ATT-{payment_attempt_id}-r{retry_count}"`. v0.16 always uses `r0` — one attempt row → one key.

> [!warning] **Don't reuse an idempotency key across user-initiated retries.** Same key only on BSS-crash-restart retries (the v1.0 path; not implemented in v0.16).

#### v1.0 will add (planned)

Two retry shapes:
1. **BSS-crash-restart retry** — same key (Stripe dedupes; we don't double-charge).
2. **User-initiated UI retry** — new key (new row, new attempt; user explicitly accepts a second charge attempt).

#### Forensic queries

```sql
-- Attempts for a customer.
SELECT id, status, amount, currency, decline_code, gateway_ref, idempotency_key, created_at
FROM payment.payment_attempt
WHERE customer_id = 'CUST-001'
ORDER BY created_at DESC;
```

```bash
# Outbound provider calls for an attempt.
bss external-calls --provider stripe --aggregate ATT-NNN
```

#### What can go wrong

- Code that constructs `f"ATT-{x}-r{n}"` outside `PaymentService.charge` — that's wrong (v0.16 doctrine).
- Idempotency key mismatch between attempt rows for the same charge — investigate; Stripe sees two calls.

#### The trap

Mock tokenizer paths use `tok_*` and bypass Stripe's idempotency table. Tests that pass against mock can hide a bug that surfaces against Stripe. Always run `make scenarios-soak` before tagging.

### 8.18 Adding a tool to `customer_self_serve` (security review)

> [!warning] Each new tool widens the chat's autonomous reach. **Don't extend the `customer_self_serve` profile without a security review.** This is a doctrine gate.

#### Pre-flight checklist

- [ ] Does it really need to be in chat? Could the customer accomplish the same via a deterministic portal route + a `requires_step_up` label?
- [ ] What happens if a prompt-injected LLM calls it with maximum mischief?
- [ ] Is the response customer-bound (carries any field of the form `customerId`, `customerEmail`, `msisdn`, `subscriptionId`, `serviceId`, `paymentMethodId`)? If yes → needs `OWNERSHIP_PATHS` entry.
- [ ] Is it a write? If yes → needs a `*.mine` / `*_for_me` wrapper. The wrapper signature must omit `customer_id` / `customer_email` / `msisdn`. The wrapper binds from `auth_context.current().actor`.
- [ ] Is it destructive (irreversible)? If yes → add to `safety.DESTRUCTIVE_TOOLS`. The wrapper still applies; the chat can call it with `/confirm`-style affordance only.

#### Implementation steps

1. Implement the canonical tool in `orchestrator/bss_orchestrator/tools/<domain>.py`.
2. Implement the `*.mine` wrapper in `orchestrator/bss_orchestrator/tools/mine_wrappers.py`. Bind `customer_id` from `auth_context`.
3. Add `OWNERSHIP_PATHS` entry in `orchestrator/bss_orchestrator/ownership.py` listing JSON paths in the response that must equal the bound actor. Use `[]` if the response carries no customer-bound fields.
4. Add the tool name to `TOOL_PROFILES["customer_self_serve"]` in `tools/_profiles.py`.
5. If destructive, add to `safety.DESTRUCTIVE_TOOLS`.
6. Update the chat system prompt (`customer_chat_prompt.py`) if the tool needs guidance.
7. Add a soak corpus probe.
8. Run `make doctrine-check` and `make scenarios-hero`.

#### Post-deploy checks

- Watch `agent.ownership_violation` structlog for the first 24 hours.
- Watch `audit.chat_usage` for spikes (new tool may surface a new failure mode that retries).

#### When to remove a tool

Symptoms: ownership-trip rate climbing; soak corpus consistently catching new probes; customer reports of cross-customer surprises.

```python
# Remove from TOOL_PROFILES["customer_self_serve"] only — leave the canonical tool.
# The chat surface no longer offers it; CSR / cockpit retain access.
```

### 8.19 Knowledge indexer reindex / Postgres pgvector prereq

The full procedure (image swap, BYOI one-liner, container bounce,
troubleshooting) lives in
[`docs/runbooks/knowledge-indexer.md`](runbooks/knowledge-indexer.md).
Quick reference:

```bash
# First-time activation (after `make migrate` lands 0022 + 0023):
make knowledge-reindex

# After a docs PR merges:
make knowledge-reindex
# → ✓ reindex complete  files=25  added=2  updated=5  deleted=0  skipped=365

# Force re-upsert (after a chunker/ranking change):
bss admin knowledge reindex --force

# Search debug surface (verifies citation quality):
bss admin knowledge search "rotate cockpit token"
bss admin knowledge search "prebaked KYC env flag" --kind doctrine
```

**Postgres prereq.** The migration's `CREATE EXTENSION IF NOT EXISTS
vector` requires pgvector to be installable. Stock `postgres:16` and
`postgres:16-alpine` images don't include it.

- **Bundled mode:** swap `image: postgres:16-alpine` →
  `image: pgvector/pgvector:pg16` (drop-in same-major; data dir
  preserved; reversible).
- **BYOI mode:** one-time `CREATE EXTENSION IF NOT EXISTS vector` on
  the target Postgres before `make migrate`.

After the swap, **bounce the BSS service containers** — their
connection pools went stale during the Postgres restart and will
500 on the first request otherwise:

```bash
docker compose restart \
    catalog crm payment com som subscription mediation rating provisioning-sim \
    portal-self-serve portal-csr
```

### 8.20 Multi-step cockpit actions (v1.5)

The REPL's natural-language grammar is an **agentic loop** as of v1.5 — one operator prompt can chain N tool calls. Useful when:

- Two-or-more reads in one breath ("show me CUST-001 then list their subscriptions")
- A compound write ("register CUST-XYZ then create order for PLAN_M")
- Discovery-then-action ("investigate CASE-042 and tell me what's going on")

**Granular mode (default — `BSS_REPL_LLM_AUTONOMY=granular`).**

Each destructive step gates separately. The operator types one NL prompt; the loop emits two `⚠ PROPOSE` panels (one per destructive write), each waiting for `/confirm`:

```
> register CUST-XYZ then issue them PLAN_M
⚠  PROPOSE: customer.create id="CUST-XYZ" email="..." 
> /confirm
(customer card renders)
⚠  PROPOSE: com.create_order customer_id="CUST-XYZ" offering="PLAN_M"
> /confirm
(order card renders, plus agent's one-sentence wrap-up)
```

**Batched mode (`BSS_REPL_LLM_AUTONOMY=batched`).**

The FIRST destructive step gates. The operator authorises the whole plan with one `/confirm`; subsequent destructive steps in the same loop execute autonomously:

```bash
export BSS_REPL_LLM_AUTONOMY=batched
# Restart the orchestrator + cockpit so the new value loads.
docker compose restart portal-csr
```

```
> register CUST-XYZ then issue them PLAN_M
⚠  PROPOSE: customer.create id="CUST-XYZ" email="..."
> /confirm
(customer card renders)
(order card renders — auto-executed; no second /confirm)
(agent's one-sentence wrap-up)
```

The mode is process-scoped — read once at orchestrator/cockpit boot via `bss_orchestrator.autonomy.read_autonomy_mode()`, cached on `app.state.autonomy_mode` for the portal and a module-level for the REPL. Unknown values fail-closed at boot (`AutonomyMisconfigured`) — same shape as `BSS_API_TOKEN=changeme`. Per-session `/autonomy {granular,batched}` override is open as a v1.5.1 question.

**Aborting a multi-step plan.** Any non-`/confirm` input replaces the pending proposal — type a new prompt to discard the plan in-flight. The pending row gets UPSERTed; nothing executes.

**Recovery semantics.** When a tool returns a structured error (`POLICY_VIOLATION`, `DESTRUCTIVE_OPERATION_BLOCKED`, `CLIENT_ERROR`) or raises (`ToolMessage.status="error"`), the loop feeds the error back to the LLM as a `role:"tool"` payload and lets it adapt. After **3 consecutive failures in a row** the loop bails with a "couldn't recover" panel — catches the Gemma thrash pattern where the same broken call keeps recurring. The 3-strike threshold lives in `bss_orchestrator.session.MAX_CONSECUTIVE_TOOL_FAILURES`.

**Chrome-filter contract.** The cockpit's own emit chrome (`(no reply)`, the empty-final recovery bubble, the citation-guard fallback, the route error fallback) is stripped from history before `astream_once(transcript=...)` sees it (`bss_cockpit.chrome_filter`). Without the filter the LLM mimics the placeholder strings; pre-v1.5 long conversations occasionally exhibited this. New cockpit fallback bubbles MUST add their prefix to `_ASSISTANT_CHROME_PREFIXES` — the inventory-lock unit test fails CI on the omission.

**When to flip to batched.** Two-step compounds where the second step is obvious from the first ("register customer then create order with the COF you just added") feel fine in batched. Investigations that may surface destructive proposals as you go ("investigate this case, then maybe close the open tickets, then maybe terminate the line") are safer in granular — you don't want a propose for `subscription.terminate` to fire just because the loop is mid-stream. Default to granular until a specific compound action repeatedly costs you a typed `/confirm`.

---

## Part 9 — Anti-patterns & doctrine ("Don'ts")

> Source-of-truth: [`CLAUDE.md`](../CLAUDE.md). This is the condensed list. Items prefixed with their version are enforced by grep guards (`make doctrine-check`).

### Cross-cutting

- **Don't put business logic in routers.** Routers → Services → Policies → Repositories. One-way.
- **Don't mix sync and async code paths.**
- **Don't catch exceptions in routers.** Let middleware handle them.
- **Don't log card numbers, tokens, full NRIC, full Ki values, or full ICCIDs beyond last-4.** structlog has a redaction filter — use it.
- **Don't add retries inside tool functions.** LangGraph supervisor handles retries.
- **Don't reach for a rules engine for rating.** A tariff is a JSON document; rating is a pure function.
- **Don't bake in channel-specific logic.** BSS-CLI is channel-agnostic.
- **Don't implement eKYC flows.** Receive attestations, record them, enforce policies.
- **Don't hardcode ports, URLs, or model names.** Everything via env.
- **Don't bypass the policy layer.** If you need to, the policy is wrong and needs amending.

### Time + state

- **(v0.5+) Don't call `datetime.now()` / `datetime.utcnow()` in business-logic paths.** Use `bss_clock.now()`. Grep guard enforces.
- **State machines, not flags.** Subscription, Order, Service, Ticket, Case — explicit FSMs with logged transitions.
- **Events are first-class.** Every meaningful state change emits a domain event AND persists to `audit.domain_event` in the same transaction.

### Snapshot pricing (v0.7+)

- **Don't call catalog active-price queries at renewal time** — read the snapshot off the subscription.
- **Don't schedule a plan change as terminate-and-recreate.** Pending fields + renewal-time pivot is the only correct path.

### Auth + session (v0.8+, v0.10+)

- **Don't accept a user-controllable `customer_id` in any post-login route handler.** Read from `request.state.customer_id`.
- **Don't store login OTPs or magic-link tokens in plaintext.** HMAC-SHA-256 with the server pepper, timing-safe compare.
- **Don't route public marketing pages through session-required middleware.**
- **Don't read cookies from a portal route handler.** `PortalSessionMiddleware` is the only path.
- **Don't accept user-controllable `customer_id` / `subscription_id` / `service_id` / `payment_method_id` in any post-login route.**
- **Don't compose multiple post-login writes in a single route handler.** Each route is one `bss-clients` write call.
- **Don't bypass step-up auth on a sensitive write.**

### Named tokens (v0.9+)

- **Don't trust an `X-BSS-Service-Identity` header.** Resolve from validated token map.
- **Don't share a named token across surfaces.**
- **Don't read `os.environ` for tokens at request time.** Load once at startup.

### Chat (v0.11+, v0.12+)

- **Only the chat surface goes through the orchestrator.** `rg 'astream_once' portals/self-serve/bss_self_serve/routes/` matches `chat.py` only.
- **Don't accept `customer_id` (or any owner-bound id) as a parameter on a `*.mine` / `*_for_me` tool.**
- **Don't extend the `customer_self_serve` tool profile without a security review.**
- **Don't let the chat surface escape its scope.**

### Cockpit (v0.13+)

- **Don't reach for OAuth/RBAC for staff.** Phase-12 staff auth retired.
- **Don't read `OPERATOR.md` or `settings.toml` outside `bss_cockpit.config`.**
- **Don't bypass `/confirm` for destructive actions in the cockpit chat.**
- **Don't add a Conversation store to a service.** Cockpit conversations live in `bss-cockpit`'s `cockpit` schema.
- **Don't accept user-controllable `session_id` outside the cockpit routes.**
- **Don't store secrets in `settings.toml`.** Secrets stay in `.env`.

### Providers (v0.14+, v0.15+, v0.16+)

- **Don't add a unified `Provider.execute()` API.** Per-domain adapter Protocols only.
- **Don't put webhook receivers behind `BSSApiTokenMiddleware`.** Provider signature is the only auth.
- **Don't log raw provider responses.** Use `bss_webhooks.redaction.redact_provider_payload()`.
- **Don't store provider config in DB** (v0.14–v0.16: `.env` only).
- **Don't silently fall back to a mock when prod creds are unset.** `select_*` raises.
- **Don't accept user-controllable provider name on a route.**
- **Don't read provider env vars at request time.** Load once at startup.

### KYC (v0.15+)

- **Don't add a `services/kyc/` container.** KYC is channel-layer.
- **Don't pass raw KYC document numbers across the BSS boundary.** Reduce to last4 + sha256 hash.
- **Don't trust a Didit attestation without a corroborating verified-webhook row.**
- **Don't silently fall back to a different KYC provider when the active one caps out.**
- **Don't accept attestations with `provider="prebaked"` in production unless `BSS_KYC_ALLOW_PREBAKED=true` explicitly.**
- **Don't enable `BSS_KYC_ALLOW_DOC_REUSE=true` in production.**
- **Don't pre-build a generic JWKS validator for Didit.** No JWS on decision API.

### Payment (v0.16+)

- **Don't render server-side card-number inputs in production-stripe mode.**
- **Don't auto-open a case on `charge.dispute.created`.** Record-only.
- **Don't auto-adjust balances on out-of-band refunds.**
- **Don't reuse an idempotency key across user-initiated retries.**
- **Don't call `mock_tokenizer.charge` directly from `payment_service.py`.**
- **Don't switch payment providers without a documented cutover.**
- **Don't ship the Stripe webhook receiver without diagnostic logging on `signature_invalid`.**
- **Don't gate webhook reconciliation on "first time we've seen this provider session."**
- **Don't treat the Stripe webhook as the primary source of truth.**
- **Don't enable `BSS_PAYMENT_ALLOW_TEST_CARD_REUSE=true` in production.**

### MNP / roaming (v0.17+)

- **Don't overload the `Case` aggregate for port requests.** Port requests are their own aggregate.
- **Don't release a ported-out MSISDN back to `available`.** Terminal status.
- **Don't add a new `event_type` for roaming.** Roaming is a per-event attribute (`roaming_indicator`).
- **Don't cascade roaming-balance exhaustion into home-data exhaustion.**
- **Don't expose port-request writes to the `customer_self_serve` profile.**

### Renewal worker (v0.18+)

- **Don't trigger renewal from anywhere except the subscription lifespan tick loop.**
- **Don't duplicate `subscription_service.renew()` logic in the worker.**

---

## Part 10 — Reference appendix

### 10.1 Glossary

| Term | Definition |
|---|---|
| **Bundle** | A subscription's allowance package (data, voice, SMS, roaming) for the current period |
| **Card-on-file (COF)** | A registered payment method that can be charged off-session |
| **CFS** | Customer-Facing Service (TMF638) |
| **Channel layer** | Mobile app / portal / retail POS / USSD — outside BSS-CLI's scope |
| **COM** | Customer Order Management (TMF622) |
| **CSR** | Customer Service Rep — historical name; in v0.13+ this is just the operator browser cockpit |
| **Doctrine** | The seven motto principles + the documented "Don't" rules in CLAUDE.md |
| **eSIM** | Embedded SIM with downloadable profile (LPA activation code; SM-DP+ simulated in v0.x) |
| **Hero scenario** | A YAML scenario tagged `hero` that exercises an end-to-end flow; gates every PR |
| **Mediation** | TMF635 online mediation — accepts one usage event at a time, enforces block-at-edge |
| **MNP** | Mobile Number Portability (port-in / port-out) |
| **Motto** | One of the seven inviolable design principles |
| **MSISDN** | Phone number (E.164) |
| **OCS** | Online Charging System — abstracted outside BSS-CLI |
| **OPERATOR.md** | Operator-editable persona prepended to every cockpit system prompt |
| **Outbox** | Simplified outbox pattern: domain event written in same DB transaction; MQ publish after commit |
| **PCI SAQ A** | Self-Assessment Questionnaire A — applies because PAN never touches BSS (Stripe Checkout hosted) |
| **Policy layer** | The single chokepoint for writes; enforces invariants before the repository is touched |
| **Port request** | First-class aggregate (`crm.port_request`) for MNP; not a Case overload |
| **RFS** | Resource-Facing Service (TMF638) |
| **Service identity** | Validated token-map-derived identity (`portal_self_serve`, `operator_cockpit`, `default`, `partner_<name>`) stamped on every audit row |
| **SOM** | Service Order Management (TMF641 + TMF638) |
| **Step-up auth** | Per-action OTP re-verification for sensitive writes |
| **TMF** | TM Forum (telco standards body) — TMF620/621/622/629/635/638/641/676/678/683 |
| **VAS** | Value-Added Service — one-shot allowance top-up |

### 10.2 Schemas at a glance

| Schema | Purpose |
|---|---|
| `audit` | `domain_event`, `chat_transcript`, `chat_usage` |
| `catalog` | `product_offering`, `product_offering_price`, `bundle_allowance`, `vas_offering`, `product_specification`, `service_specification`, `product_to_service_mapping` |
| `cockpit` | `session`, `message`, `pending_destructive` (v0.13) |
| `com` | `product_order`, `product_order_item`, `order_state_history` |
| `crm` | `customer`, `contact_medium`, `kyc_attestation`, `case`, `case_note`, `ticket`, `interaction`, `port_request` |
| `integrations` | `external_call`, `webhook_event`, `kyc_webhook_corroboration` |
| `inventory` | `msisdn_pool`, `esim_profile_pool` |
| `knowledge` | (post-v0.x; pgvector RAG) |
| `mediation` | `usage_event` |
| `payment` | `payment_method`, `payment_attempt`, `customer` (provider customer cache), `payment_method_state_history` |
| `portal_auth` | `identity`, `login_token`, `session`, `login_attempt`, `email_change_pending`, `portal_action` |
| `provisioning` | `task`, `task_state_history`, `fault_injection_config` |
| `rating` | (computed; little persistent state) |
| `service_inventory` | `service`, `service_state_history` |
| `som` | `service_order`, `service_order_item` |
| `subscription` | `subscription`, `bundle_balance`, `subscription_state_history`, `vas_purchase` |

### 10.3 Domain events (cheat-sheet)

| Event | Emitted by | Why |
|---|---|---|
| `customer.created` | crm | New customer signed up |
| `customer.kyc_attested` | crm | KYC attestation recorded |
| `customer.closed` | crm | Customer-record closed |
| `kyc.cap_exhausted` | self-serve portal | Didit free-tier exhausted (high priority for ops) |
| `case.opened` / `transitioned` / `note_added` / `priority_updated` / `closed` | crm | Case lifecycle |
| `ticket.opened` / `assigned` / `transitioned` / `resolved` / `closed` / `cancelled` | crm | Ticket lifecycle |
| `port_request.created` / `approved` / `completed` / `rejected` | crm | MNP lifecycle |
| `inventory.msisdn.seeded_from_port_in` / `ported_out` / `pool_low` | crm/inventory | MSISDN lifecycle |
| `order.created` / `submitted` / `in_progress` / `completed` / `cancelled` | com | COM lifecycle |
| `service_order.created` / `started` / `completed` / `failed` | som | SOM lifecycle |
| `service.activated` / `suspended` / `terminated` | som | Service lifecycle |
| `subscription.activated` / `renewed` / `renew_failed` / `renewal_skipped` / `exhausted` / `blocked` / `unblocked` / `terminated` / `vas_purchased` / `plan_change_scheduled` / `plan_changed` / `plan_change_cancelled` / `plan_change_payment_failed` / `price_migration_scheduled` / `price_migrated` | subscription | Subscription lifecycle |
| `usage.recorded` / `rated` / `rejected` | mediation/rating | Usage lifecycle |
| `payment.charged` / `declined` / `errored` / `refunded` / `dispute_opened` / `attempt_state_drift` | payment | Payment lifecycle |
| `payment_method.created` / `removed` / `default_set` / `cutover_invalidated` | payment | COF lifecycle |
| `provisioning.task.completed` / `failed` / `stuck` / `resolved` / `retried` | provisioning | Provisioning lifecycle |
| `notification.requested` | various | A user-facing notification should fire (consumed by email adapter in v0.14+) |
| `agent.ownership_violation` | orchestrator | Chat output trip-wire (P0 forensics) |

### 10.4 Tool profiles (cockpit vs chat)

| Profile | Members | Writes? | Owner-bound? |
|---|---|---|---|
| `operator_cockpit` | All registry tools **except** `*.mine` / `*_for_me` wrappers | Yes (with `/confirm` for destructive) | No |
| `customer_self_serve` | Public catalog reads + `*.mine` / `*_for_me` wrappers + `case.open_for_me` + `case.list_for_me` | Yes via wrappers (with destructive list still gating) | Yes (`auth_context.actor`) |
| `default` | Same as `operator_cockpit` (orchestrator default) | — | — |

`validate_profiles()` runs at orchestrator startup and refuses to boot if:
- Any tool in `operator_cockpit` is `*.mine` / `*_for_me`.
- Any tool in `customer_self_serve` lacks an `OWNERSHIP_PATHS` entry.
- Any `*.mine` / `*_for_me` tool's signature includes `customer_id`, `customer_email`, `msisdn`.
- Coverage drift: a tool in `TOOL_REGISTRY` not in any profile.

### 10.5 Portal routes (every one)

#### Self-serve (`localhost:9001`)

**Public:** `/welcome`, `/plans`, `/auth/*`, `/static/*`, `/portal-ui/static/*`, `/terms`, `/privacy`, `/signup/step/kyc/callback`, `/webhooks/*`, `/health`.

**Login-gated:**
- `/` (dashboard)
- `/signup/{plan_id}`, `/signup/{plan_id}/msisdn`, `/signup/{plan_id}/progress`, `/signup/step/kyc`, `/signup/step/kyc/poll`, `/signup/step/cof`, `/signup/step/cof/checkout-init`, `/signup/step/cof/checkout-return`, `/signup/step/order`, `/signup/step/poll`
- `/activation/{order_id}`, `/activation/{order_id}/status`
- `/confirmation/{subscription_id}`
- `/esim/{subscription_id}`
- `/billing/history`
- `/top-up`, `/top-up/success` (POST `/top-up`)
- `/subscription/{id}/cancel` (GET + POST)
- `/plan/change`, `/plan/change/cancel`, `/plan/change/scheduled`
- `/profile/contact`, `/profile/contact/{name|phone|address}/update`, `/profile/contact/email/{change|verify|cancel}`
- `/payment-methods`, `/payment-methods/add`, `/payment-methods/add/checkout-init`, `/payment-methods/add/checkout-return`, `/payment-methods/{pm_id}/{remove|set-default}`
- `/chat`, `/chat/widget`, `/chat/message`, `/chat/reset`, `/chat/events/{session_id}`
- `/session/{session_id}` (session API)

**Webhooks** (provider signature only):
- `POST /webhooks/resend`
- `POST /webhooks/didit`
- `POST /webhooks/stripe` (in payment service, port 8003, not portal)

#### Operator browser cockpit (`localhost:9002`)

| Route | What |
|---|---|
| `GET /` | Sessions index |
| `POST /cockpit/new` | New session |
| `GET /cockpit/{id}` | Chat thread |
| `POST /cockpit/{id}/turn` | Append turn |
| `POST /cockpit/{id}/reset` | Clear messages |
| `POST /cockpit/{id}/confirm` | Mark confirm |
| `POST /cockpit/{id}/focus` | Set/clear focus |
| `GET /cockpit/{id}/events` | SSE stream |
| `GET /search?q=...` | Customer search |
| `POST /search/start_session` | Open session pinned to customer |
| `GET /case/{case_id}` | Case detail |
| `GET /settings` | View OPERATOR.md + settings.toml |
| `POST /settings/operator` | Save OPERATOR.md |
| `POST /settings/config` | Save settings.toml |
| `GET /health` | Liveness |

### 10.6 IDs (prefixed strings)

Internal DB uses UUIDs; surface IDs are prefixed strings.

| Prefix | Aggregate |
|---|---|
| `CUST-NNN` | Customer |
| `ORD-NNN` | Customer Order |
| `SO-NNN` | Service Order |
| `SVC-NNN` | Service |
| `SUB-NNN` | Subscription |
| `CASE-NNN` | Case |
| `TKT-NNN` | Ticket |
| `PORT-NNN` | Port request |
| `PTK-NNN` | Provisioning task |
| `IDT-NNN` | External-call aggregate id |
| `ATT-NNN` | Payment attempt |
| `SES-YYYYMMDD-<8 hex>` | Cockpit session |
| `pi_*` | Stripe PaymentIntent |
| `pm_*` | Stripe PaymentMethod |
| `cus_*` | Stripe Customer |
| `cs_*` | Stripe Checkout Session |
| `whsec_*` | Webhook signing secret |

### 10.7 Doc map (where to look for what)

| If you want to know... | Read |
|---|---|
| The seven mottoes | [`CLAUDE.md`](../CLAUDE.md) |
| Container topology, AWS path, footprint budgets | [`ARCHITECTURE.md`](../ARCHITECTURE.md) |
| Why a decision was made | [`DECISIONS.md`](../DECISIONS.md) (append-only log) |
| The full tool catalogue with descriptions | [`TOOL_SURFACE.md`](../TOOL_SURFACE.md) |
| Schemas, columns, migrations | [`DATA_MODEL.md`](../DATA_MODEL.md) |
| Phase-by-phase build plan | [`phases/V0_X_Y.md`](../phases/) |
| Roadmap toward v1.0 | [`ROADMAP.md`](../ROADMAP.md) |
| Contributing, branching, testing | [`CONTRIBUTING.md`](../CONTRIBUTING.md) |
| **This handbook** | You're here |

---

> **Maintenance note.** This handbook synthesizes [`CLAUDE.md`](../CLAUDE.md), [`ARCHITECTURE.md`](../ARCHITECTURE.md), [`DECISIONS.md`](../DECISIONS.md), and the per-runbook docs in [`docs/runbooks/`](runbooks/). When those drift, this drifts. Refresh on every minor release.
