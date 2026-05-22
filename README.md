# BSS-CLI

> The entire BSS, in a terminal. SID-aligned. TMF-compliant. LLM-native. eSIM-first.

A complete reference Business Support System for a small mobile prepaid MVNO that runs from a single terminal command. Nine TMF-compliant service containers (Catalog, CRM with Cases/Tickets/Port-requests, Payment, COM, SOM, Subscription, Mediation, Rating, Provisioning-sim) plus two web portals (self-serve customer + operator cockpit). Every operation is a tool the LLM can call; the primary UI is the `bss` CLI plus ASCII visualizations and a scoped chat surface in the customer portal.

For engineers learning telco BSS/OSS, for a small MVNO that wants a deployable MVP, and as a substrate for agentic experiments against realistic telco operations. **eSIM-only, bundled-prepaid, block-on-exhaust, card-on-file mandatory.** eKYC, real-customer UI, network elements, batch CDR, and OCS protocols are intentionally out of scope (channel-layer concerns).

**Status (v1.1.3 — Promo codes):** promotions via the optional loyalty-cli adapter — non-targeted typed codes + targeted eligibility-gated codes, composed on the price snapshot, charged effective for 1/N/perpetual periods. loyalty-cli is optional (BSS-CLI runs fully without it). v1.1.3 makes a promo claim failure (e.g. an exhausted code) degrade to full price instead of bricking the order; v1.1.2 fixed the signup live-discount preview. Built on v1.0 — the wrap of the v0.x arc. All three real-provider integrations are live (Resend email, Didit KYC, Stripe Checkout). Telco hygiene gaps are closed (MNP port-in/port-out, MSISDN replenishment, roaming as a product). Renewals fire automatically on their period boundary via an in-process worker; customers get a reminder email ~24h before. The operator cockpit runs single-operator-by-design behind a secure perimeter — REPL canonical, browser veneer over the same Postgres-backed `Conversation` store. The cockpit knowledge tool (v0.20) answers how-to questions from `docs/HANDBOOK.md` + runbooks + CLAUDE.md with cited anchors (Postgres FTS by default, optional pgvector hybrid). The only mocked surface left for production is SM-DP+ (real eSIM provisioning is NDA-gated).

## Screenshots

### Operator cockpit — REPL (canonical surface)

The `bss` REPL is the cockpit. Type natural language; the agent calls tools; results render as ASCII cards. Slash commands (`/ports`, `/360`, `/focus`, `/confirm`) cover deterministic operator flows.

![bss REPL banner](docs/screenshots/bss_repl_v1.jpg)

### Operator cockpit — browser veneer

Same `Conversation` store as the REPL; exit `bss`, open `localhost:9002`, see the same turns. No login wall (single-operator-by-design behind a secure perimeter).

- **Sessions index** — `localhost:9002/`. Recent conversations + customer search + new-conversation CTA.

  ![cockpit sessions index](docs/screenshots/portal_csr_cockpit_sessions_v1.jpg)

- **Live conversation** — agent renders ASCII tool cards inline (catalog VAS list, plan detail, etc.). Destructive actions propose first, wait for `/confirm`.

  ![cockpit conversation](docs/screenshots/portal_csr_cockpit_session_v1.jpg)

### Self-serve portal (customer-facing)

- **Public landing** — `localhost:9001/welcome`.

  ![self-serve welcome](docs/screenshots/portal_self_serve_welcome_v1.jpg)

- **Plan picker** — `localhost:9001/plans`. Three plans, all four allowance rows aligned (Data / Voice / SMS / Roaming). PLAN_S has no roaming included; PLAN_M ships 500 mb, PLAN_L ships 2 GB.

  ![self-serve plans](docs/screenshots/portal_self_serve_plans_v1.jpg)

### Distributed trace (`bss trace`)

- **Signup chain swimlane** — every span across COM → SOM → Inventory → Provisioning-sim → Subscription → Payment, ASCII rendered:

  ![bss trace swimlane](docs/screenshots/bss_trace_swimlane_v0_2.png)

> Re-capture against your own stack: `uv run python docs/screenshots/capture_portals.py` for the web surfaces; see [`docs/screenshots/CAPTURE.md`](docs/screenshots/CAPTURE.md) for prereqs and the manual REPL capture.

## How writes flow

Every BSS write goes through the per-service policy layer. Three trigger paths feed it:

- **Direct via `bss-clients`** — every CLI/REPL command, the entire signup funnel, every post-login self-serve route, and every read. Sub-second, deterministic, no LLM.
- **Orchestrator-mediated via `astream_once`** — the customer-portal chat surface (only orchestrator-mediated route post-v0.11). Wraps a LangGraph ReAct agent over the same tool registry. The same policy chokepoint enforces both paths, so audit + attribution stay coherent.
- **In-process tick loops** — the v0.18 renewal worker fires automatic renewals and sends upcoming-renewal reminder emails on a 60-second tick. `FOR UPDATE SKIP LOCKED` makes it multi-replica safe by construction.

The audit log gets a coherent attribution on every write: `actor`, `channel` (`portal-self-serve` / `portal-csr` / `portal-chat` / `cli` / `system:renewal_worker`), and `service_identity` (`portal_self_serve` / `default` / etc. via the v0.9 named-token perimeter).

## What's in the box (v0.7 → v1.0)

| Release | What landed |
|---|---|
| **v0.7** | Catalog versioning + plan changes; subscription price snapshotted at order time; renewal reads the snapshot, not the catalog |
| **v0.8** | Self-serve portal authentication — email + magic-link / OTP, server-side sessions, public-route allowlist, step-up scaffolding |
| **v0.9** | Named tokens at the BSS perimeter; `service_identity` propagation through audit + structlog + OTel |
| **v0.10** | Authenticated post-login customer self-serve writes go direct (chat stays orchestrator-mediated); per-resource ownership policies + step-up gating |
| **v0.11** | Signup funnel goes direct (sub-second per step). Chat is the only orchestrator-mediated route |
| **v0.12** | Chat scoping — `customer_self_serve` profile + `*.mine` wrappers + ownership trip-wire + per-customer caps + 5-category escalation. 14-day soak |
| **v0.13** | Operator cockpit. CLI REPL canonical, browser veneer over a shared Postgres-backed `Conversation` store. v0.5 staff-auth retired |
| **v0.14** | Real-provider integration arc begins: per-domain adapter Protocols, `integrations` schema for forensic external-call + webhook-event logging, ResendEmailAdapter for transactional auth mail |
| **v0.15** | KYC (Didit) + the eSIM-provider seam. Channel-layer KYC; BSS only verifies signed attestations + corroboration |
| **v0.16** | Payment (Stripe Checkout + webhook reconciliation). PCI scope guard refuses to boot in production-stripe mode if a card-number `<input>` survives in any rendered template |
| **v0.17** | Telco hygiene release. MNP (port-in / port-out via `crm.port_request`), MSISDN replenishment (`bss inventory msisdn add-range` + low-watermark event), roaming as a product (`data_roaming` allowance type, `VAS_ROAMING_1GB` top-up) |
| **v0.18** | Automated subscription-renewal worker. Three sweeps per tick: renew due / skip blocked-overdue / send upcoming-renewal email. Multi-replica safe via `FOR UPDATE SKIP LOCKED` from day one |
| **v0.20** | Cockpit knowledge tool — Tier-0 FTS over `docs/HANDBOOK.md` + CLAUDE.md + runbooks; LLM cites anchors, citation guard catches un-cited claims. Catalog `--data-roaming-mb` flag closes the v0.17 admin gap. Postgres image moves to `pgvector/pgvector:pg16` |
| **v1.0** | General Availability — wrap of the v0.x arc. All seven principles intact under load: bundled-prepaid, card-on-file, block-on-exhaust, CLI-first / LLM-native, TMF-compliant, lightweight (~2.65 GiB all-in incl. infra; p99 internal API < 50 ms), write-through policy. Real-provider integrations live; SM-DP+ remains NDA-gated mock. Single-Postgres / schema-per-domain holds; documented split path is ready when traffic demands it |
| **v1.1** | Promo codes — *integrate, don't build*. The separate `samurai-bot/loyalty-cli` is the entitlement engine; BSS owns only the money terms (`catalog.promotion`) and composes over HTTP. Non-targeted typed codes (`SUMMER25`) + targeted codes gated by a BSS eligibility list (operator pre-assigns via `bss promo assign`; auto-applies for eligible customers, rejected for others). Discount composes on the lowest-active price; charged effective for 1 / N / perpetual periods, decremented at renewal, ended by a plan change. Claim-at-activation (a provisioning failure never burns a code; a payment decline revokes it). Operator-only tools (`bss promo`, `promo.*`) — customers type a code, never issue one. **loyalty-cli is an optional adapter — BSS-CLI runs fully without it (promos just off); set `BSS_LOYALTY_*` to enable.** Runbook: [`docs/runbooks/promo-codes.md`](docs/runbooks/promo-codes.md) |

Full per-release narratives in [`phases/V0_X_Y.md`](phases/) and [`phases/V1_1_0.md`](phases/V1_1_0.md). Post-v1.0 work and non-goals live in [`ROADMAP.md`](ROADMAP.md).

## Quick start

### Prerequisites

- Docker + Docker Compose
- Python 3.12 + [uv](https://docs.astral.sh/uv/)
- An OpenRouter API key (or any OpenAI-compatible endpoint) for the orchestrator-mediated chat + REPL

### Bring-your-own-infra (BYOI — Postgres / RabbitMQ already running)

> **v0.20+ prereq.** The cockpit knowledge tool's table uses pgvector.
> Most managed Postgres tiers (RDS, Cloud SQL, Aiven, Neon) offer it
> as a switch-on extension. Run **`CREATE EXTENSION IF NOT EXISTS
> vector`** once on your BSS database before `make migrate`.
> See [`docs/runbooks/knowledge-indexer.md`](docs/runbooks/knowledge-indexer.md)
> for the full procedure.

```bash
git clone <repo>
cd bss-cli
cp .env.example .env

# Generate a real BSS_API_TOKEN; the sentinel value rejects on startup.
sed -i "s/^BSS_API_TOKEN=changeme$/BSS_API_TOKEN=$(openssl rand -hex 32)/" .env

# Edit .env: BSS_DB_URL, BSS_RABBITMQ_URL, BSS_LLM_API_KEY,
# optionally BSS_OTEL_EXPORTER_OTLP_ENDPOINT (e.g. http://tech-vm:4318)

# v0.20+ — install pgvector once on the target Postgres:
psql "$BSS_DB_URL" -c "CREATE EXTENSION IF NOT EXISTS vector;"

docker compose up -d         # 9 services + 2 portals
make migrate                  # Alembic on the existing Postgres (currently at 0023)
make seed                     # 3 plans + 4 VAS offerings + 1000 MSISDNs + 1000 eSIM profiles
make knowledge-reindex        # populate the cockpit's doc corpus (372 chunks)
bss                           # opens the cockpit REPL
```

### All-in-one (bundled infra)

The bundled `docker-compose.infra.yml` ships **`pgvector/pgvector:pg16`** as the
Postgres image (v0.20+; drop-in same-major replacement for stock
`postgres:16-alpine`). pgvector activates automatically — no manual `CREATE
EXTENSION` needed.

```bash
docker compose -f docker-compose.yml -f docker-compose.infra.yml up -d
make migrate
make seed
make knowledge-reindex              # populate the cockpit's doc corpus
bss                                 # cockpit REPL
open http://localhost:9001/         # self-serve portal
open http://localhost:9002/         # operator cockpit (browser veneer)
open http://localhost:16686/        # Jaeger UI (traces)
open http://localhost:3000/         # Metabase (analytics)
```

### First commands worth running

```bash
make scenarios                   # 19 hero scenarios — sanity-check the install (~100s)
make doctrine-check              # 16 grep guards (clock, OTel, channel, renewal, knowledge, ...)
bss subscription show SUB-0001
bss inventory msisdn list --prefix 9000 --limit 5
bss trace for-order ORD-0001
bss admin knowledge search "rotate cockpit token"   # v0.20+ doc-corpus search
```

## Documentation map

- [`CLAUDE.md`](CLAUDE.md) — project doctrine; read first
- [`docs/HANDBOOK.md`](docs/HANDBOOK.md) — **v0.20+ single-file operator handbook** consolidating setup, env vars, providers, personas, domain features, and every runbook into one Obsidian-friendly reference. The cockpit knowledge tool indexes this
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — topology, call patterns, deployability matrix, AWS deployment path
- [`DATA_MODEL.md`](DATA_MODEL.md) — schemas + tables + relationships
- [`TOOL_SURFACE.md`](TOOL_SURFACE.md) — every LLM tool with arg shape and return shape
- [`DECISIONS.md`](DECISIONS.md) — non-obvious architectural choices, append-only
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — phase discipline, DECISIONS pattern, test conventions
- [`ROADMAP.md`](ROADMAP.md) — shipped + post-v1.0 + non-goals
- [`phases/`](phases/) — per-release build plans (PHASE_01 → PHASE_10, V0_2_0 → V0_20_0)
- [`docs/runbooks/`](docs/runbooks/) — operational procedures (knowledge-indexer + pgvector prereq, Jaeger BYOI, API token rotation, snapshot regen, MNP port flows, Stripe cutover, payment idempotency, chat ownership trip, chat caps, chat-escalated case triage, chat transcript retention, three-provider sandbox soak)

## Tracing with `bss trace`

Every service exports OpenTelemetry traces to Jaeger. Read them three ways:

```bash
bss trace for-order ORD-0014           # ASCII swimlane in the terminal — see screenshot
bss trace for-subscription SUB-0007
bss trace get 4a8f9e2c0123…            # by trace id

open http://localhost:16686/            # all-in-one — Jaeger UI
open http://tech-vm:16686/              # BYOI; see docs/runbooks/jaeger-byoi.md
```

For BYOI installs, run a single-container Jaeger on a separate host and point `BSS_OTEL_EXPORTER_OTLP_ENDPOINT` at it. The full ASCII swimlane is taller than the cropped README screenshot — run the command in your terminal to see every span.

## Hero scenarios

Living regression suite under `scenarios/*.yaml`. **17 scenarios tagged `hero`** as of v0.18 — every release adds one for the headline feature. Each scenario starts with `admin.reset_operational_data` + `clock.freeze_at` for determinism.

```bash
make scenarios                                 # all 17 (~95s wall clock)
bss scenario list scenarios                    # inventory
bss scenario validate scenarios/*.yaml         # parse-check
bss scenario run scenarios/<name>.yaml         # single run
bss scenario run-all scenarios --tag hero      # tag-filtered
```

LLM-driven scenarios (the few that ask the agent to reason rather than dispatch deterministically) should pass three runs in a row before tagging — model variance is real and the gate exists to catch flakes.

### v0.12 14-day soak

`scenarios/soak/run_soak.py` provisions N synthetic customers and runs them in parallel for D simulated days under an accelerated frozen clock. Each customer fires events probabilistically (10% dashboard / 5% chat / 1% escalation / 0.5% top-up / 0.1% cross-customer probe). Soak gates: zero ownership-check trips, zero cross-customer leaks, chat-usage drift ≤ 5%, p99 chat latency under 5s (alarm at 15s), bounded transcript-table growth.

```bash
# Smoke (validates wiring; ~1 min wall clock)
uv run python -m scenarios.soak.run_soak --customers 2 --days 1

# Substantive run (default report path: soak/report-v0.12.md)
uv run python -m scenarios.soak.run_soak --customers 30 --days 14
```

The v0.12 baseline run is checked in at [`soak/report-v0.12.md`](soak/report-v0.12.md). A soak re-run on v1.0 was the gate that anchored the GA tag.

## License

Apache-2.0
