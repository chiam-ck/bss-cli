.PHONY: help up up-all up-minimal up-core down build test fmt lint migrate seed doctrine-check rust-migrate rust-seed rust-fmt rust-lint rust-test rust-doctrine-check knowledge-reindex reset-db scenarios scenarios-hero scenarios-site-demo dev-mailbox-dir

help:
	@echo "  up                  — FULL stack (9 services + 2 portals), BYOI: uses your EXTERNAL Postgres/RabbitMQ/Jaeger. ← default"
	@echo "  up-all              — FULL stack + BUNDLED LOCAL Postgres/RabbitMQ/Jaeger (only if you have no external infra)"
	@echo "  up-minimal          — subset: catalog + crm + payment only"
	@echo "  up-core             — subset: up-minimal + com + som + subscription + provisioning-sim"
	@echo "  down                — stop everything (app + any bundled infra)"
	@echo "  build               — build all Rust service + portal images"
	@echo "  migrate             — bss admin migrate (Rust sqlx)"
	@echo "  seed                — bss admin seed (Rust). 3 plans + 4 VAS + 1000 MSISDNs + 1000 eSIMs"
	@echo "  knowledge-reindex   — v0.20+ reindex doc corpus into knowledge.doc_chunk"
	@echo "  test                — cargo test --workspace"
	@echo "  fmt                 — cargo fmt --check"
	@echo "  lint                — cargo clippy -D warnings"
	@echo "  scenarios           — run every scenario in ./scenarios (including LLM ask: steps)"
	@echo "  scenarios-hero      — run only the hero ship-gate scenarios"
	@echo "  doctrine-check      — Rust grep guards over the workspace"

# 2.0: the all-Rust images are the default stack (`make up*` runs the Rust overlay
# over the base compose; both build contexts are the repo root post-2.0).
#
# `up`  vs `up-all` — the ONLY difference is where Postgres/RabbitMQ/Jaeger live:
#   `up`     = full app stack, BYOI  → your EXTERNAL infra (BSS_DB_URL / BSS_MQ_URL / OTLP in .env).
#   `up-all` = full app stack + BUNDLED LOCAL infra (adds docker-compose.infra.yml).
# The "-all" means "+ bundled infra", NOT "more app containers" — both start all 11.
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.rust.yml

up: dev-mailbox-dir
	$(COMPOSE) up -d

up-all: dev-mailbox-dir
	$(COMPOSE) -f docker-compose.infra.yml up -d

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
	$(COMPOSE) up -d catalog crm payment

up-core:
	$(COMPOSE) up -d catalog crm payment com som subscription provisioning-sim

down:
	$(COMPOSE) -f docker-compose.infra.yml down

build:
	$(COMPOSE) build

# 2.0: the canonical dev-loop targets drive the Rust workspace (the Python oracle
# was retired at v2.0.0 — see docs/PYTHON-ORACLE.md).
test: rust-test

doctrine-check: rust-doctrine-check

fmt: rust-fmt

lint: rust-lint

# --- Rust dev loop. cargo isn't on PATH by default in make's shell — source the
# toolchain env first.
CARGO_ENV := . "$$HOME/.cargo/env" 2>/dev/null || true

rust-fmt:
	@$(CARGO_ENV); cargo fmt --all --check

rust-lint:
	@$(CARGO_ENV); cargo clippy --workspace --all-targets --all-features -- -D warnings

rust-test:
	@$(ENV_SOURCE); $(CARGO_ENV); cargo test --workspace

# Rust counterpart to `doctrine-check`: the greppable doctrine guards over rust/.
# (Structural guards are enforced by construction/clippy; "test" guards run via
# `rust-test`. See phases/2.0/02-TECH-MAPPING.md §4.)
rust-doctrine-check:
	@bash scripts/rust_doctrine_check.sh

# mypy can't run as a single `mypy .` in this workspace: every service
# deliberately shares the `app` package name (same reason `make test`
# isolates PYTHONPATH per directory), so root-level checking dies on
# duplicate-module errors before reporting anything. Per-component runs
# work but strict mode is deeply red today (~120-270 errors per
# component, 2026-06-12 baseline) — kept OUT of `lint` until a typing
# project burns that down. Run per component, e.g.:
#   uv run mypy packages/bss-clients/bss_clients

# --- Data Model ---

# Source .env (if present) inside every recipe that needs DB/MQ creds. `set -a`
# exports every var until `set +a`, so children (alembic, psql, bss-seed) inherit them.
ENV_SOURCE := if [ -f .env ]; then set -a; . ./.env; set +a; fi

# `migrate` runs the Rust sqlx migrator (Alembic retired at v2.0.0).
migrate: rust-migrate


# Phase 8 (2.0): the sqlx migrator replaces Alembic for the all-Rust stack. Fresh
# install → applies rust/migrations/; existing (Alembic-created) DB → run once with
# `-- --baseline` to stamp the baseline as applied without re-running it. See
# docs/runbooks/rust-schema-baseline.md.
rust-migrate:
	@$(ENV_SOURCE); . "$$HOME/.cargo/env" 2>/dev/null || true; cargo run --quiet -p bss-cli -- admin migrate $(ARGS)

seed: rust-seed


rust-seed:
	@$(ENV_SOURCE); . "$$HOME/.cargo/env" 2>/dev/null || true; cargo run --quiet -p bss-cli -- admin seed


# v0.20+ — operator-driven doc-corpus reindex into knowledge.doc_chunk.
# Runs on-demand (no file-watcher in containers). Idempotent —
# unchanged sections skip via mtime + content_hash dedup.
knowledge-reindex:
	@$(ENV_SOURCE); . "$$HOME/.cargo/env" 2>/dev/null || true; cargo run --quiet -p bss-cli -- admin knowledge reindex

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
	. "$$HOME/.cargo/env" 2>/dev/null || true; cargo run --quiet -p bss-cli -- scenario run-all scenarios $(1)
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

reset-db:
	@$(ENV_SOURCE); \
	PSQL_URL=$$(echo "$$BSS_DB_URL" | sed 's|+asyncpg||'); \
	psql "$$PSQL_URL" -c "DROP SCHEMA IF EXISTS crm, catalog, inventory, payment, order_mgmt, service_inventory, provisioning, subscription, mediation, billing, audit, portal_auth, cockpit, integrations, knowledge CASCADE;"; \
	psql "$$PSQL_URL" -c "DELETE FROM public.alembic_version;" 2>/dev/null || true; \
	psql "$$PSQL_URL" -c "DROP TABLE IF EXISTS public._sqlx_migrations;" 2>/dev/null || true; \
	$(MAKE) migrate; \
	$(MAKE) seed; \
	$(MAKE) knowledge-reindex
