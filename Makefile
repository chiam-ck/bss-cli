.PHONY: help up up-all up-minimal up-core down build test fmt lint migrate seed seed-demo seed-demo-reset loyalty-reset demo-restore knowledge-reindex reset-db check-clock doctrine-check python-check scenarios scenarios-hero scenarios-site-demo e2e e2e-down

help:
	@echo "  up                  — 10 BSS services (BYOI Postgres/RabbitMQ)"
	@echo "  up-all              — services + Postgres + RabbitMQ + Jaeger"
	@echo "  up-minimal          — catalog + crm + payment only"
	@echo "  up-core             — minimal + com + som + subscription + provisioning-sim"
	@echo "  down                — stop everything"
	@echo "  build               — build all service images"
	@echo "  migrate             — alembic upgrade head"
	@echo "  seed                — seed reference data (3 plans + 4 VAS + 1000 MSISDNs + 1000 eSIMs)"
	@echo "  seed-demo           — synced demo seed: 3 customers + 2 promos across BSS + loyalty"
	@echo "  seed-demo-reset     — surgical reverse of seed-demo (demo-prefix only; spares operator data)"
	@echo "  loyalty-reset       — TRUNCATE loyalty.* + audit.* and re-stamp alembic head"
	@echo "  demo-restore        — THE button: reset-db + loyalty-reset + seed-demo (BSS + loyalty in lockstep)"
	@echo "  knowledge-reindex   — v0.20+ reindex doc corpus into knowledge.doc_chunk"
	@echo "  test                — run pytest"
	@echo "  fmt                 — format with ruff"
	@echo "  lint                — lint with ruff + mypy"
	@echo "  scenarios           — run every scenario in ./scenarios (including LLM ask: steps)"
	@echo "  scenarios-hero      — run only the three hero ship-gate scenarios"
	@echo "  e2e                 — v1.4 Playwright suite in mock-providers mode (CLEAN=1 for full wipe)"
	@echo "  e2e-down            — manually tear down a stuck e2e override stack"
	@echo "  check-clock         — grep guard: all datetime.now sites route through bss-clock"
	@echo "  doctrine-check      — run all v0.6+ grep guards (clock, channel, portals, no-bypass)"
	@echo "  python-check        — warn if active Python is outside the supported 3.12 range"

up: dev-mailbox-dir
	docker compose up -d

up-all: dev-mailbox-dir
	docker compose -f docker-compose.yml -f docker-compose.infra.yml up -d

# v0.8 — pre-create the host bind-mount dir for the portal dev mailbox.
# If Docker auto-creates it, it lands as root:root 755 and the portal
# container (uid 1000) can't write — POST /auth/login 500s with
# PermissionError. Creating it owned by the calling user (or 1000)
# avoids the trap. We use 0777 so it works regardless of host uid
# layout (this is dev-only state; production uses real SMTP).
dev-mailbox-dir:
	@mkdir -p .dev-mailbox
	@chmod 0777 .dev-mailbox 2>/dev/null || true

up-minimal:
	docker compose up -d catalog crm payment

up-core:
	docker compose up -d catalog crm payment com som subscription provisioning-sim

down:
	docker compose -f docker-compose.yml -f docker-compose.infra.yml down

build:
	docker compose build

python-check:
	@# Project targets Python 3.12 (CLAUDE.md "Tech stack"). Newer minors
	@# (e.g. 3.14) work mostly but have surfaced regressions:
	@# `asyncio.get_event_loop()` removed in 3.14, Pydantic V1 deprecation
	@# warnings under LangChain. Earlier minors (<3.12) lack syntax we use.
	@# This is a warn-only check — never fails the build, just flags drift.
	@v=$$(uv run python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'); \
	case "$$v" in \
		3.12) echo "✓ python $$v (supported)" ;; \
		3.13) echo "⚠ python $$v — newer than 3.12 target; should work but untested. See CLAUDE.md tech-stack." ;; \
		*)   echo "⚠ python $$v — outside supported 3.12 range. Recreate venv: uv python install 3.12.13 && uv venv --python 3.12.13" ;; \
	esac

test:
	@failed=0; \
	for dir in packages/bss-clients packages/bss-admin packages/bss-clock packages/bss-events packages/bss-telemetry packages/bss-middleware packages/bss-webhooks packages/bss-portal-ui packages/bss-portal-auth packages/bss-cockpit services/catalog services/crm services/payment services/subscription services/com services/som services/provisioning-sim services/mediation services/rating orchestrator cli portals/self-serve portals/csr scenarios/soak; do \
		printf "\n══ $$dir ══\n"; \
		PYTHONPATH=$$dir/tests:$$dir:$$PYTHONPATH uv run pytest $$dir/tests/ -v -m "not integration" || failed=1; \
	done; \
	if [ $$failed -eq 1 ]; then printf "\n✗ Some suites failed\n"; exit 1; \
	else printf "\n✓ All suites passed\n"; fi

check-clock:
	@# Every business-logic site must route through bss_clock.now().
	@# bss-clock impl, the `bss clock` cmd surface, the renderer wall-clock fallback,
	@# tests, and any line carrying `# noqa: bss-clock` are exempt.
	@hits=$$(grep -rnE "datetime\.(utcnow|now)" --include="*.py" \
		services/ packages/ orchestrator/ cli/bss_cli/ portals/ 2>/dev/null \
		| grep -v "packages/bss-clock/" \
		| grep -v "cli/bss_cli/commands/clock.py" \
		| grep -v "/tests/" \
		| grep -v "# noqa: bss-clock" \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ business logic must import clock_now from bss_clock:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ all datetime.now sites route through bss_clock"

doctrine-check: check-clock
	@# v0.6 wrapper around all grep-guard doctrine checks.
	@# Adds: portal-handlers don't write via bss-clients on the chat
	@# surface (v0.11+ doctrine, narrowed from the earlier v0.4–v0.10
	@# rule that also covered signup), and OTel imports stay out of
	@# services/policies (v0.2).
	@#
	@# v0.10+ — post-login self-serve writes go direct via bss-clients
	@# (CLAUDE.md "v0.10+ / authenticated post-login customer self-serve").
	@# v0.11+ — signup writes go direct via bss-clients
	@# (CLAUDE.md "v0.11+ / chat only"). The route files below are the
	@# carve-out; they enforce ownership + step-up where applicable.
	@# Adding a new direct-write route requires extending this list AND
	@# (for sensitive post-login routes) the SENSITIVE_ACTION_LABELS
	@# catalogue in security.py. The chat surface (when it lands in
	@# v0.12+) stays orchestrator-mediated and would NOT join this list.
	@# v1.6.1 — cockpit CRM screens write direct via bss-clients
	@# (CLAUDE.md "Cockpit CRM screens"); destructive/money verbs gate
	@# on the two-step confirm field, pinned by
	@# portals/csr/tests/test_routes_crm.py. Only routes/cockpit.py
	@# remains orchestrator-mediated on the CSR portal.
	@hits=$$(grep -rnE '\.(create|charge|purchase_vas|terminate|add_card|remove_method|cancel)\(' \
		--include='*.py' portals/*/bss_*/routes/ 2>/dev/null \
		| grep -v 'session_store.create\|store.create\|ask_about_customer' \
		| grep -v 'portals/self-serve/bss_self_serve/routes/signup\.py' \
		| grep -v 'portals/self-serve/bss_self_serve/routes/top_up\.py' \
		| grep -v 'portals/self-serve/bss_self_serve/routes/payment_methods\.py' \
		| grep -v 'portals/self-serve/bss_self_serve/routes/esim\.py' \
		| grep -v 'portals/self-serve/bss_self_serve/routes/cancel\.py' \
		| grep -v 'portals/self-serve/bss_self_serve/routes/profile\.py' \
		| grep -v 'portals/self-serve/bss_self_serve/routes/billing\.py' \
		| grep -v 'portals/self-serve/bss_self_serve/routes/plan_change\.py' \
		| grep -v 'portals/csr/bss_csr/routes/customers\.py' \
		| grep -v 'portals/csr/bss_csr/routes/cases\.py' \
		| grep -v 'portals/csr/bss_csr/routes/orders\.py' \
		| grep -v 'portals/csr/bss_csr/routes/catalog\.py' \
		| grep -v 'portals/csr/bss_csr/routes/subscriptions\.py' \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ portal route handlers must not call mutating bss-clients on the chat surface:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ chat-only orchestrator-mediation; v0.10 post-login + v0.11 signup routes carved out"
	@hits=$$(grep -rn 'from opentelemetry' --include='*.py' \
		services/*/app/services/ services/*/app/policies/ 2>/dev/null \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ OTel imports leaked into service/policy layer:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ OTel surfaces stay out of services/ and policies/"
	@hits=$$(grep -rn 'campaignos' services/ cli/ orchestrator/ portals/ packages/ scenarios/ 2>/dev/null \
		| grep -v '/alembic/versions/' \
		| grep -v '# noqa: campaignos' \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ campaign OS reference leaked:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ campaign OS untouched"
	@# v0.7+ — renewal must read the price snapshot, never query the active catalog.
	@# `get_active_price` is the active-aware lookup forbidden in the renewal stack.
	@# `get_offering_price` (no `_active_`) is the snapshot resolve and is allowed —
	@# it fetches a price row by id without any time filter.
	@hits=$$(awk '/async def renew\b/,/^    async def [a-z]/' \
		services/subscription/app/services/subscription_service.py 2>/dev/null \
		| grep -nE 'get_active_price' \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ subscription.renew() must not query catalog active-price APIs:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ renewal reads snapshot, not catalog"
	@# v0.9+ — service_identity is resolved from token validation, never from
	@# a sibling header. A grep for any "X-BSS-Service-Identity" reference in
	@# Python source must stay empty. The negative-control test in the
	@# middleware suite asserts the runtime behaviour; this guard catches
	@# accidental introductions during code review.
	@hits=$$(grep -rn 'X-BSS-Service-Identity' --include='*.py' \
		services/ packages/ orchestrator/ portals/ cli/ 2>/dev/null \
		| grep -v '/tests/' \
		| grep -v '# noqa: service-identity-header' \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ caller-asserted X-BSS-Service-Identity header leaked:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ no caller-asserted service-identity header"
	@# v0.9+ — tokens are loaded once at startup and cached. Per-request
	@# os.environ reads of any BSS_*_API_TOKEN are forbidden outside the
	@# centralized loader (api_token.py), the AuthProvider classes
	@# (auth.py / auth_provider.py / clients.py), test fixtures (conftest.py,
	@# test_*.py), and the orchestrator session (which resolves identity
	@# tokens for astream_once).
	@hits=$$(grep -rnE 'os\.environ.*BSS_.*API_TOKEN' --include='*.py' \
		services/ packages/ orchestrator/ portals/ cli/ 2>/dev/null \
		| grep -v 'packages/bss-middleware/bss_middleware/api_token\.py' \
		| grep -v 'packages/bss-clients/bss_clients/auth\.py' \
		| grep -v 'orchestrator/bss_orchestrator/session\.py' \
		| grep -v '/conftest\.py' \
		| grep -v '/test_[^/]*\.py' \
		| grep -v '# noqa: token-runtime-read' \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ request-time os.environ token reads forbidden:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ tokens loaded once at startup, cached"
	@# v0.10+ — customer_id must come from request.state.customer_id, never
	@# from form / body / query / path on a post-login route. CLAUDE.md
	@# "(v0.10+) Don't accept user-controllable customer_id..." anti-pattern.
	@hits=$$(grep -rnE 'customer_id\s*[:=]\s*(Form|Body|Query|Path)\(' \
		--include='*.py' portals/self-serve/bss_self_serve/routes/ 2>/dev/null \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ customer_id taken from form/body/query/path in a post-login route:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ customer_id bound from request.state, not user-controllable"
	@# v0.11+ — the orchestrator streaming entrypoint stays inside the
	@# chat route only. Signup + post-login self-serve routes go direct
	@# via bss-clients (CLAUDE.md "(v0.11+ / chat only)" anti-pattern).
	@# The chat route (`routes/chat.py`) lands in v0.12+; until then the
	@# whitelist is empty and this guard rejects every match.
	@hits=$$(grep -rn 'astream_once' \
		--include='*.py' portals/self-serve/bss_self_serve/routes/ 2>/dev/null \
		| grep -v 'routes/chat\.py' \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ astream_once leaked into a non-chat route (signup + post-login go direct):"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ astream_once stays in chat route only (signup + post-login are direct)"
	@# v0.16+ — Stripe sandbox fixtures must never carry real card numbers,
	@# customer ids, payment method ids, payment intent ids, charge ids,
	@# event ids, or live API keys. Spec: phases/V0_16_0.md line 61.
	@# Track 0's redactor remaps every real id to a 12-char placeholder
	@# (e.g. `pi_FX_PI001`) so this 14+-char regex stays empty.
	@hits=$$(grep -rnE 'sk_live_|pi_[0-9A-Za-z]{14}|pm_[0-9A-Za-z]{14}|cus_[0-9A-Za-z]{14}|cn_[0-9A-Za-z]{14}|evt_[0-9A-Za-z]{14}' \
		services/payment/tests/fixtures/ 2>/dev/null \
		| grep -v 'README\.md' \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ stripe sandbox fixture contains a real id or live key:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ stripe sandbox fixtures redacted (no real ids or live keys)"
	@# v0.17+ — pure rating function stays unaware of roaming. Routing
	@# happens in the rating consumer (services/rating/app/events/consumer.py)
	@# AFTER rate_usage returns. CLAUDE.md "(v0.17+) Don't add a new
	@# event_type for roaming." anti-pattern.
	@hits=$$(grep -n 'data_roaming' \
		services/rating/app/domain/rating.py 2>/dev/null \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ data_roaming leaked into pure rating function:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ rate_usage stays unaware of roaming (routing lives in consumer)"
	@# v0.17+ — ported_out is terminal. No code path may flip a
	@# ported_out row back to available. CLAUDE.md "(v0.17+) Don't
	@# release a ported-out MSISDN back to available."
	@hits=$$(grep -nE "(ported_out.*'available'|'available'.*ported_out|set status\s*=\s*'available'.*ported_out)" \
		services/crm/app/repositories/msisdn_repo.py 2>/dev/null \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ ported_out row reset to available — terminal status doctrine breached:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ ported_out is terminal (no available-loopback)"
	@# v0.18+ — renewal worker is confined to subscription lifespan.
	@# Tick loop + sweep helpers live in workers/renewal.py and are
	@# referenced by dependencies.py (lifespan) and renewal_admin.py
	@# (admin tick endpoint) only. Any other reference is a parallel
	@# scheduler and breaks the multi-replica safety guarantee that
	@# lives in FOR UPDATE SKIP LOCKED.
	@hits=$$(grep -rnE '(_renewal_tick_loop|_sweep_due|_sweep_skipped)' \
		--include='*.py' \
		services/ packages/ orchestrator/ portals/ cli/ 2>/dev/null \
		| grep -v 'services/subscription/app/workers/renewal\.py' \
		| grep -v 'services/subscription/app/dependencies\.py' \
		| grep -v 'services/subscription/app/api/renewal_admin\.py' \
		| grep -v '/tests/' \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ renewal worker referenced outside subscription lifespan:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ renewal worker stays in subscription lifespan only"
	@# v0.18.1+ — service config.py never hardcodes a version string.
	@# `version: str = BSS_RELEASE` (sourced from bss_models) so a single
	@# bump in packages/bss-models/bss_models/__init__.py propagates to
	@# every service's /health endpoint on the next container recreate.
	@# Any literal "version: str = "X.Y.Z"" is drift waiting to happen.
	@hits=$$(grep -rnE 'version:\s*str\s*=\s*"[0-9]' --include='config.py' \
		services/ portals/ packages/ 2>/dev/null \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ service version hardcoded — should source BSS_RELEASE:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ all service versions sourced from BSS_RELEASE"
	@# v0.20+ — knowledge tools live in operator_cockpit profile only.
	@# Customer chat must NOT receive RAG over operator runbooks (would
	@# leak destructive-flow hints + perimeter posture). The profile
	@# entry block in tools/_profiles.py is the source of truth; any
	@# `knowledge.search` / `knowledge.get` literal outside that file +
	@# tests + the tool module itself is drift.
	@hits=$$(grep -rnE '"knowledge\.(search|get)"' \
		--include='*.py' \
		orchestrator/ portals/ cli/ packages/ 2>/dev/null \
		| grep -v 'orchestrator/bss_orchestrator/tools/_profiles\.py' \
		| grep -v 'orchestrator/bss_orchestrator/tools/knowledge\.py' \
		| grep -v '/tests/' \
		| grep -v 'cli/bss_cli/commands/admin_knowledge\.py' \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ knowledge tool literal referenced outside profile / tool / tests:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ knowledge tools stay in operator_cockpit profile only"
	@# v0.20+ — phases/V0_*.md must NOT appear as a quoted entry in the
	@# indexer allowlist. Phase docs are historical build plans and
	@# mislead the LLM. The allowlist (INDEXED_PATHS) is in
	@# packages/bss-knowledge/bss_knowledge/paths.py — quoted string
	@# entries only; doc-references in docstrings/comments are fine.
	@hits=$$(grep -nE '"phases/V0_' \
		packages/bss-knowledge/bss_knowledge/paths.py 2>/dev/null \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ phases/V0_*.md leaked into knowledge indexer allowlist:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ phases/V0_*.md stays out of the knowledge index"
	@# v1.2 — for services on the resilient pipeline (com, som, subscription)
	@# the outbox relay is the SINGLE publisher: their event publishers only
	@# stage the audit.domain_event row and must NOT call exchange.publish
	@# (the publish-before-commit hazard the relay removes). The only
	@# legitimate exchange.publish lives in bss_events/relay.py. Other services
	@# still inline-publish (best-effort) until they're migrated; expand this
	@# list as each converts.
	@hits=$$(grep -rn 'exchange\.publish' \
		services/com/app/events/publisher.py \
		services/som/app/events/publisher.py \
		services/subscription/app/events/publisher.py 2>/dev/null \
		| grep -v 'default_exchange\.publish' \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ a converted publisher calls exchange.publish — stage only; the outbox relay publishes:"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ outbox relay is the single publisher for com/som/subscription (stage only)"
	@# v1.2 — COM/SOM consumers must go through bss_events.bind_consumer
	@# (retry + DLQ + inbox dedup). A bare `message.process()` is the old
	@# drop-on-exception pattern that stranded paid orders (v1.1.3).
	@hits=$$(grep -rn 'message\.process(' \
		services/com/app/events/ services/som/app/events/ 2>/dev/null \
		|| true); \
	if [ -n "$$hits" ]; then \
		echo "✗ COM/SOM consumer uses bare message.process() — use bss_events.bind_consumer (retry+DLQ+inbox):"; \
		echo "$$hits"; \
		exit 1; \
	fi; \
	echo "✓ COM/SOM consumers go through the safe bind_consumer helper"

fmt:
	uv run ruff format .

lint:
	uv run ruff check .

# mypy can't run as a single `mypy .` in this workspace: every service
# deliberately shares the `app` package name (same reason `make test`
# isolates PYTHONPATH per directory), so root-level checking dies on
# duplicate-module errors before reporting anything. Per-component runs
# work but strict mode is deeply red today (~120-270 errors per
# component, 2026-06-12 baseline) — kept OUT of `lint` until a typing
# project burns that down. Run per component, e.g.:
#   uv run mypy packages/bss-clients/bss_clients
lint-types:
	@failed=0; \
	for dir in packages/bss-clients packages/bss-clock packages/bss-cockpit services/crm/app orchestrator/bss_orchestrator; do \
		printf "\n══ mypy $$dir ══\n"; \
		uv run mypy $$dir || failed=1; \
	done; \
	exit $$failed

# --- Data Model ---

# Source .env (if present) inside every recipe that needs DB/MQ creds. `set -a`
# exports every var until `set +a`, so children (alembic, psql, bss-seed) inherit them.
ENV_SOURCE := if [ -f .env ]; then set -a; . ./.env; set +a; fi

migrate:
	@$(ENV_SOURCE); cd packages/bss-models && uv run --package bss-models alembic upgrade head

seed:
	@$(ENV_SOURCE); uv run --package bss-seed python -m bss_seed.main

# v1.3.1 — synced demo seed. Creates a small coherent demo dataset across
# BSS (customers, promotions, eligibility) and loyalty-cli (registered
# customers, OD/promo_code, upfront-issued offers). Idempotent. Without
# BSS_LOYALTY_API_TOKEN set, the customer half still runs; the promo lane
# skips with a log line (BSS-only mode is the supported fallback).
#
# v1.3.2 — the loyalty base url + token are read by the SHELL here (from
# .env via ENV_SOURCE) and passed to Python as CLI flags. Python never
# reads BSS_LOYALTY_API_TOKEN from os.environ — keeps the doctrine guard
# "tokens loaded once at startup" honest without an in-source noqa.
seed-demo:
	@$(ENV_SOURCE); uv run --package bss-seed python -m bss_seed.demo seed \
	    --loyalty-base-url "$$BSS_LOYALTY_BASE_URL" \
	    --loyalty-token "$$BSS_LOYALTY_API_TOKEN"

# Reverse of seed-demo. Demo-prefix surgical: unassigns the targeted promo
# (loyalty revoke + BSS delete), drops the two demo promotions, and removes
# the demo customers from BOTH systems. Never touches operator data.
seed-demo-reset:
	@$(ENV_SOURCE); uv run --package bss-seed python -m bss_seed.demo reset \
	    --loyalty-base-url "$$BSS_LOYALTY_BASE_URL" \
	    --loyalty-token "$$BSS_LOYALTY_API_TOKEN"

# v1.3.1 — full wipe of loyalty's data (TRUNCATE loyalty.* + audit.* schemas,
# re-stamp alembic head). Reads LOYALTY_DB_URL from env, or peeks into the
# running loyalty-http container's env. Companion to `make reset-db` on the
# BSS side.
loyalty-reset:
	@$(ENV_SOURCE); uv run --package bss-seed python -m bss_seed.demo loyalty-wipe

# THE single button: full clean slate on BOTH systems + the synced demo seed
# repopulated. Use after a demo session that left messy test data, or any time
# you want to return to a known-good golden state. Order matters:
#   1. `make reset-db`       — BSS schemas dropped + re-migrated + `make seed` (refdata)
#   2. `make loyalty-reset`  — loyalty + audit schemas TRUNCATEd, alembic re-stamped
#   3. `make seed-demo`      — 3 demo customers, 1 public + 1 targeted promo,
#                              2 customers assigned to the targeted (upfront
#                              pairing minted in loyalty per v1.3.0)
# After this both systems are in lockstep with a small, coherent demo dataset.
demo-restore:
	@$(ENV_SOURCE); $(MAKE) reset-db
	@$(ENV_SOURCE); $(MAKE) loyalty-reset
	@$(ENV_SOURCE); $(MAKE) seed-demo
	@echo
	@echo "✓ demo restored. BSS + loyalty in sync; 3 customers, 2 promos, 2 VIP assignments."

# v0.20+ — operator-driven doc-corpus reindex into knowledge.doc_chunk.
# Runs on-demand (no file-watcher in containers). Idempotent —
# unchanged sections skip via mtime + content_hash dedup.
knowledge-reindex:
	@$(ENV_SOURCE); uv run --package bss-cli bss admin knowledge reindex

# Hero / general scenarios drive auth flows by reading OTPs from the
# dev-mailbox file that LoggingEmailAdapter writes. They also run
# against synthetic identities that don't have real KYC documents OR
# real Stripe-side `cus_*`/`pm_*` records. So when an operator runs
# with .env pointing at real providers, we temporarily flip every
# customer-facing provider to its in-process equivalent for the
# duration of the run, then restore on exit:
#
#   BSS_PORTAL_EMAIL_PROVIDER  resend → logging
#   BSS_PORTAL_KYC_PROVIDER    didit  → prebaked  (+ ALLOW_PREBAKED=true)
#   BSS_PAYMENT_PROVIDER       stripe → mock      (v0.16)
#
# Each flip recreates the affected containers (env vars are read at
# lifespan startup, NOT baked into the image — so just a recreate is
# enough, no rebuild needed).
define SCENARIOS_RUN
	@$(ENV_SOURCE); \
	prev=$$(grep -E '^BSS_PORTAL_EMAIL_PROVIDER=' .env | tail -1 | cut -d= -f2-); \
	prev_kyc=$$(grep -E '^BSS_PORTAL_KYC_PROVIDER=' .env | tail -1 | cut -d= -f2-); \
	prev_payment=$$(grep -E '^BSS_PAYMENT_PROVIDER=' .env | tail -1 | cut -d= -f2-); \
	had_allow_prebaked=$$(grep -cE '^BSS_KYC_ALLOW_PREBAKED=' .env); \
	if [ "$$prev" != "logging" ] && [ "$$prev" != "noop" ]; then \
		printf "▶ scenarios: flipping BSS_PORTAL_EMAIL_PROVIDER=%s → logging for portal container\n" "$$prev"; \
		sed -i.bak 's|^BSS_PORTAL_EMAIL_PROVIDER=.*|BSS_PORTAL_EMAIL_PROVIDER=logging|' .env; \
		recreate_email=1; \
	else \
		recreate_email=0; \
	fi; \
	if [ -n "$$prev_kyc" ] && [ "$$prev_kyc" != "prebaked" ]; then \
		printf "▶ scenarios: flipping BSS_PORTAL_KYC_PROVIDER=%s → prebaked for the run\n" "$$prev_kyc"; \
		sed -i.bak2 's|^BSS_PORTAL_KYC_PROVIDER=.*|BSS_PORTAL_KYC_PROVIDER=prebaked|' .env; \
		if [ $$had_allow_prebaked -eq 0 ]; then \
			printf 'BSS_KYC_ALLOW_PREBAKED=true\n' >> .env; \
		fi; \
		recreate_kyc=1; \
	else \
		recreate_kyc=0; \
	fi; \
	if [ -n "$$prev_payment" ] && [ "$$prev_payment" != "mock" ]; then \
		printf "▶ scenarios: flipping BSS_PAYMENT_PROVIDER=%s → mock for the run\n" "$$prev_payment"; \
		sed -i.bak3 's|^BSS_PAYMENT_PROVIDER=.*|BSS_PAYMENT_PROVIDER=mock|' .env; \
		recreate_payment=1; \
	else \
		recreate_payment=0; \
	fi; \
	if [ $$recreate_email -eq 1 ] || [ $$recreate_kyc -eq 1 ] || [ $$recreate_payment -eq 1 ]; then \
		docker compose up -d --force-recreate portal-self-serve crm payment >/dev/null 2>&1 || true; \
		trap 'if [ '"$$recreate_email"' -eq 1 ]; then sed -i "s|^BSS_PORTAL_EMAIL_PROVIDER=.*|BSS_PORTAL_EMAIL_PROVIDER='"$$prev"'|" .env; rm -f .env.bak; printf "▶ scenarios: restored BSS_PORTAL_EMAIL_PROVIDER=%s\n" "'"$$prev"'"; fi; if [ '"$$recreate_kyc"' -eq 1 ]; then sed -i "s|^BSS_PORTAL_KYC_PROVIDER=.*|BSS_PORTAL_KYC_PROVIDER='"$$prev_kyc"'|" .env; rm -f .env.bak2; if [ '"$$had_allow_prebaked"' -eq 0 ]; then sed -i "/^BSS_KYC_ALLOW_PREBAKED=/d" .env; fi; printf "▶ scenarios: restored BSS_PORTAL_KYC_PROVIDER=%s\n" "'"$$prev_kyc"'"; fi; if [ '"$$recreate_payment"' -eq 1 ]; then sed -i "s|^BSS_PAYMENT_PROVIDER=.*|BSS_PAYMENT_PROVIDER='"$$prev_payment"'|" .env; rm -f .env.bak3; printf "▶ scenarios: restored BSS_PAYMENT_PROVIDER=%s\n" "'"$$prev_payment"'"; fi; docker compose up -d --force-recreate portal-self-serve crm payment >/dev/null 2>&1 || true' EXIT INT TERM; \
	fi; \
	uv run bss scenario run-all scenarios $(1)
endef

scenarios:
	$(call SCENARIOS_RUN,)

scenarios-hero:
	$(call SCENARIOS_RUN,--tag hero)

# v1.6 — rebuild the bss-cli.com screenshot dataset (five personas,
# staggered bundle burn, cases/tickets in three lifecycle states).
# Resets operational data first; same provider-flip wrapper as the
# hero suite.
scenarios-site-demo:
	$(call SCENARIOS_RUN,--tag site_demo)

# v1.4 — Playwright end-to-end suite. Brings up the stack with
# docker-compose.e2e.yml overlaid (payment=mock, kyc=prebaked,
# email=logging, esim=sim), runs `demo-restore` for clean seed,
# then pytest with HTML + JUnit reports + per-spec traces. Tear-down
# fires on EXIT/INT/TERM so Ctrl-C still cleans up:
#
#   1. compose down with the e2e override   (drops mock-only state)
#   2. `make seed-demo-reset` as a backstop (surgical PROMO_DEMO_* /
#      *.demo@bss-cli.local sweep — also catches e2e-* leftovers via
#      the prefix-scoped reset_e2e_data path once specs are wired in)
#   3. `docker compose up -d`               (restore normal stack)
#
# Reports land in docs/e2e-reports/<UTC-ts>/ — gitignored except
# README.md. v1.4.1 produces VISUAL artefacts (screenshots + video +
# Playwright trace) per spec, plus a top-level index.html gallery
# that links everything. Open the index.html in any browser to review
# what the test saw, in order, end-to-end. JUnit XML stays for CI
# ingestion later. Pass CLEAN=1 to do a destructive `down -v` (drops
# volumes) instead of the soft down.
#
# Pre-condition: your .env should have mock/no-creds for Stripe / Didit
# / Resend. A real `sk_live_*` in .env will still cause the payment
# service startup template-scan to refuse to boot — the override pins
# `BSS_PAYMENT_PROVIDER=mock` but doesn't strip a live secret key.
e2e:
	@mkdir -p docs/e2e-reports
	@ts=$$(date -u +%Y%m%dT%H%M%SZ); \
	out="docs/e2e-reports/$$ts"; \
	mkdir -p "$$out"; \
	export BSS_E2E_REPORT_DIR="$$PWD/$$out"; \
	down_cmd="docker compose -f docker-compose.yml -f docker-compose.e2e.yml down --remove-orphans"; \
	if [ "$$CLEAN" = "1" ]; then down_cmd="$$down_cmd -v"; fi; \
	trap 'echo; echo "--- tearing down e2e stack ---"; \
	      eval "$$down_cmd" >/dev/null 2>&1 || true; \
	      $(MAKE) seed-demo-reset >/dev/null 2>&1 || true; \
	      docker compose up -d >/dev/null 2>&1 || true; \
	      echo; echo "--- e2e gallery: file://'"$$PWD"'/'"$$out"'/index.html ---"' EXIT INT TERM; \
	echo "--- bringing up e2e stack (override: docker-compose.e2e.yml) ---"; \
	docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --wait; \
	echo "--- seeding demo data ---"; \
	$(MAKE) demo-restore; \
	echo "--- running playwright suite (artefacts → $$out) ---"; \
	($(ENV_SOURCE); export BSS_E2E_REPORT_DIR="$$BSS_E2E_REPORT_DIR"; \
	 uv run --package bss-e2e pytest packages/bss-e2e/tests/ \
	     --junitxml="$$out/junit.xml")

# v1.5 — run the e2e suite with the cockpit autonomy mode set to
# "batched" so test_v15_compound_action_batched (skipped under the
# default granular mode) exercises. Same shape as `make e2e`; just
# pre-exports the env var before invoking it. Granular-only specs
# (test_v15_compound_action_granular) will skip cleanly because
# their setup observes the same env.
e2e-batched:
	@BSS_REPL_LLM_AUTONOMY=batched $(MAKE) e2e

# Manual escape-hatch — if a previous `make e2e` left the override
# stack up (e.g. SIGKILL), run this to drop it and bring the normal
# stack back.
e2e-down:
	@docker compose -f docker-compose.yml -f docker-compose.e2e.yml down --remove-orphans
	@docker compose up -d
	@echo "✓ e2e override torn down; normal stack restored."

reset-db:
	@$(ENV_SOURCE); \
	PSQL_URL=$$(echo "$$BSS_DB_URL" | sed 's|+asyncpg||'); \
	psql "$$PSQL_URL" -c "DROP SCHEMA IF EXISTS crm, catalog, inventory, payment, order_mgmt, service_inventory, provisioning, subscription, mediation, billing, audit, portal_auth, cockpit, integrations, knowledge CASCADE;"; \
	psql "$$PSQL_URL" -c "DELETE FROM public.alembic_version;" 2>/dev/null || true; \
	$(MAKE) migrate; \
	$(MAKE) seed; \
	$(MAKE) knowledge-reindex
