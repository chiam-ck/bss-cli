# Fresh-machine setup (bundled dev stack)

Bringing BSS-CLI up from a bare clone on a new Linux/WSL2 machine, as a
local all-in-one dev stack (bundled Postgres/RabbitMQ/Jaeger, logging
email, prebaked KYC, mock payment). Distilled from a real fresh-machine
bring-up on 2026-08-07 — including the traps that aren't visible from
the README's quickstart.

Nothing here needs root: Docker Compose and Rust both install
user-locally, and the workspace has no native-TLS build deps (rustls
throughout — `gcc` is the only host build tool required).

## 0. Prerequisites

| Need | Check | Install without sudo |
|---|---|---|
| Docker daemon | `docker ps` | (daemon itself needs admin once — Docker Desktop / distro package) |
| Compose v2 plugin | `docker compose version` | `mkdir -p ~/.docker/cli-plugins && curl -fsSL -o ~/.docker/cli-plugins/docker-compose https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 && chmod +x ~/.docker/cli-plugins/docker-compose` |
| Rust (stable) | `cargo --version` | `curl -fsSL https://sh.rustup.rs \| sh -s -- -y --default-toolchain stable --profile minimal --component rustfmt --component clippy` |
| gcc (linker) | `which gcc` | usually preinstalled; otherwise distro package |

## 1. `.env` — host-oriented, with real secrets

```bash
cp .env.example .env
sed -i "s/^BSS_API_TOKEN=changeme$/BSS_API_TOKEN=$(openssl rand -hex 32)/" .env
sed -i "s/^BSS_PORTAL_TOKEN_PEPPER=changeme$/BSS_PORTAL_TOKEN_PEPPER=$(openssl rand -hex 32)/" .env
# Set BSS_LLM_API_KEY to your OpenRouter key (chat/REPL only; rest of
# the stack runs without it).
```

**The trap:** `.env` is read by BOTH the containers (compose `env_file`)
and the host-built `bss` CLI (`make seed` / `migrate` /
`knowledge-reindex` all source it). The example ships container
hostnames (`postgres:5432`), which the host cannot resolve. Make `.env`
host-oriented instead:

```bash
sed -i "s|^BSS_DB_URL=.*|BSS_DB_URL=postgresql+asyncpg://bss:bss@localhost:5432/bss|" .env
sed -i "s|^BSS_MQ_URL=.*|BSS_MQ_URL=amqp://guest:guest@localhost:5672/|" .env
sed -i "s|^BSS_OTEL_EXPORTER_OTLP_ENDPOINT=.*|BSS_OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318|" .env
```

…and give the containers their in-network names back via the local
override in step 2.

Optional dev affordances: `BSS_ALLOW_ADMIN_RESET=true` (needed by
`make reset-db` + scenarios) and `BSS_ENABLE_TEST_ENDPOINTS=true`
(enables the dev `bss payment add-card` path against the mock
tokenizer). If you don't run loyalty-cli, comment out both
`BSS_LOYALTY_*` lines — the promo subsystem switches off cleanly.

## 2. `docker-compose.local.yml` — the machine-local override

Create this file (untracked; add it to `.git/info/exclude`). It does
three jobs:

1. restores container-side infra hostnames (because `.env` is now
   host-oriented);
2. points the portals' `BSS_LLM_BASE_URL` at OpenRouter directly (the
   base compose pins a `litellm-proxy` container most machines don't
   run — see `docs/runbooks/litellm-proxy.md`);
3. hardens startup: restart policies for the infra containers (the
   infra compose file sets none) and health-gated `depends_on` so app
   services can't race RabbitMQ on a cold boot. **Without this gate,
   services that start before RabbitMQ is accepting connections log
   `mq.consumer.setup_failed` once and never retry** — orders then
   stall at COM with 0 service orders while their events sit staged in
   the outbox.

```yaml
x-local-infra-env: &local-infra-env
  BSS_DB_URL: postgresql+asyncpg://bss:bss@postgres:5432/bss
  BSS_MQ_URL: amqp://guest:guest@rabbitmq:5672/
  BSS_OTEL_EXPORTER_OTLP_ENDPOINT: http://jaeger:4318

x-local-infra-deps: &local-infra-deps
  postgres: { condition: service_healthy }
  rabbitmq: { condition: service_healthy }

services:
  postgres:
    restart: unless-stopped
  rabbitmq:
    restart: unless-stopped
  jaeger:
    restart: unless-stopped
  catalog:
    depends_on: *local-infra-deps
    environment: *local-infra-env
  crm:
    depends_on: *local-infra-deps
    environment: *local-infra-env
  payment:
    depends_on: *local-infra-deps
    environment: *local-infra-env
  com:
    depends_on: *local-infra-deps
    environment: *local-infra-env
  som:
    depends_on: *local-infra-deps
    environment: *local-infra-env
  subscription:
    depends_on: *local-infra-deps
    environment: *local-infra-env
  mediation:
    depends_on: *local-infra-deps
    environment: *local-infra-env
  rating:
    depends_on: *local-infra-deps
    environment: *local-infra-env
  provisioning-sim:
    depends_on: *local-infra-deps
    environment: *local-infra-env
  portal-self-serve:
    depends_on: *local-infra-deps
    environment:
      <<: *local-infra-env
      BSS_LLM_BASE_URL: https://openrouter.ai/api/v1
  portal-csr:
    depends_on: *local-infra-deps
    environment:
      <<: *local-infra-env
      BSS_LLM_BASE_URL: https://openrouter.ai/api/v1
```

Then teach the Makefile to pick it up — change the `COMPOSE` line to:

```make
COMPOSE := docker compose -f docker-compose.yml $(shell [ -f docker-compose.local.yml ] && echo -f docker-compose.local.yml)
```

(This is a no-op on machines without the file, and it makes
`make up-all` / `down` / `build` all include the override.)

## 3. Build, up, migrate, seed

```bash
make build                                  # 11 images; first build ≈ 30–40 min
cargo build --release -p bss-cli            # host CLI for migrate/seed/REPL
make up-all                                 # app + bundled infra
set -a; source .env; set +a
./target/release/bss admin migrate
make seed                                   # 3 plans, 4 VAS, 1000 MSISDNs, 1000 eSIMs
make knowledge-reindex                      # cockpit doc-search corpus
```

Optional: a wrapper so `bss` works from any shell/directory —
`~/.local/bin/bss` (on PATH in most distros):

```bash
#!/usr/bin/env bash
set -euo pipefail
BSS_REPO="$HOME/Codes/bss-cli"      # adjust
set -a; . "$BSS_REPO/.env"; set +a
exec "$BSS_REPO/target/release/bss" "$@"
```

## 4. Verify

- all `/health` endpoints 200: ports 8001–8008, 8010
- portals: `http://localhost:9001` (self-serve), `:9002` (cockpit),
  `:16686` (Jaeger), `:15672` (RabbitMQ, guest/guest)
- `bss catalog list` renders the three seeded plans
- portal login OTPs appear in `.dev-mailbox/portal-mailbox.log`
  (`tail -F` it — the file is created on first send)

## Known gotchas on a fresh machine

- **RabbitMQ startup race** — prevented by the `depends_on` gates
  above for compose-driven starts. Docker-daemon restarts (host
  reboot) do NOT honor `depends_on`; if orders stall after a reboot
  (COM order `in_progress`, 0 SOs, `mq.consumer.setup_failed` in
  logs), run `docker compose restart` on the app services (or
  `make down && make up-all`).
- **Don't restart `portal-self-serve` while a customer is
  mid-checkout** — the signup flow state is in-memory, and the
  "Activating…" page polls it; a restart strands that page forever.
  The order itself completes fine; the QR is always at
  `/esim/<subscription-id>`.
- **WSL2**: the stack survives closing the terminal, but WSL doesn't
  autostart on Windows boot — the stack returns when you next open a
  WSL shell (restart policies bring the containers back).
- **Jaeger traces are in-memory** — gone on every restart, by design.
- `make scenarios*` **wipes all customer data** — by design; see the
  warning banner it prints.
