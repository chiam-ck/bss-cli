# Local development

This setup runs the nine BSS services, two portals, Postgres with pgvector,
RabbitMQ, and Jaeger on this computer. Compose uses the `bss-cli-dev` project
and publishes ports on loopback only. Database and message-broker data persist
in Docker volumes when the stack stops.

Prerequisites on Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 rustup pkg-config libssl-dev build-essential
sudo usermod -aG docker "$USER"
# Log out and log back in for the group membership to take effect.
rustup toolchain install stable --profile minimal --component rustfmt --component clippy
```

Before running setup, create the ignored repo-root `.env` from `.env.example`
(`cp .env.example .env; chmod 600 .env`). Replace the example BSS API tokens and
portal token pepper with distinct randomly generated values. Set
`BSS_ENV=development`, payment provider `mock`, KYC provider `prebaked`, email
provider `logging`, and eSIM provider `sim`. Clear the LLM key and optional
loyalty credentials until those integrations are configured. The example
container database/message-broker URLs match the bundled infrastructure.

No external API key is required for these local flows. Chat requires a funded
LLM provider key. The scripts use existing configuration; they do not prompt
for or generate credentials, and they do not install host prerequisites.

From the repository root:

```bash
scripts/dev setup                 # build CLI, start infra, migrate, seed, index, build/start apps
scripts/dev status                # container health and local ports
scripts/dev bss catalog list      # invoke the CLI with host-side infrastructure URLs
scripts/dev bss                   # operator REPL (chat requires an LLM key)
scripts/dev logs portal-csr       # recent logs for one service
scripts/dev down                  # stop containers; retain database volumes
scripts/dev up                    # start existing images
scripts/dev build catalog         # rebuild a service after source changes
scripts/dev compose up -d catalog # recreate it with the rebuilt image
```

The `.env` infrastructure URLs use Compose DNS names. Use `scripts/dev bss`
for the host CLI; it substitutes loopback URLs without changing container
configuration. Rebuild the CLI after changing its Rust source:
`cargo build --locked --release -p bss-cli`.

- Customer portal: <http://localhost:9001/>
- Operator cockpit: <http://localhost:9002/>
- Jaeger traces: <http://localhost:16686/>
- RabbitMQ management: <http://localhost:15672/> (`guest` / `guest`)

Local email is written to `.dev-mailbox/portal-mailbox.log`. It can contain login
links and OTPs; read it locally when testing signup. Operator settings live in
`.bss-cli/`. The development overlay runs the cockpit container as the host user
so those settings remain writable.

For public HTTPS and real-provider setup, see [DEMO-INTEGRATIONS.md](DEMO-INTEGRATIONS.md).
Keep local credentials out of Git and Docker build contexts. After switching to
the public overlay, use `scripts/demo` for Compose operations so it stays applied.

## Verification on 2026-09-13

- Rust release CLI built from the checked-in lockfile; all 11 app images built.
- All 14 containers running; the 13 containers with healthchecks passed.
- Schema migration, reference-data seed, and documentation indexing succeeded
  (26 files, 419 chunks). CLI plan listing and documentation search passed.
- `customer_signup_and_exhaust`: all 13 steps passed, including mock payment,
  activation, block-on-exhaust, and VAS top-up. Its order trace had 34 spans
  across eight services, with zero errors.
- `portal_self_serve_signup_direct`: all 16 steps passed, including local OTP
  login, signup, mock KYC/payment, and active subscription verification.
- Both scenarios ran from temporary copies with data reset and clock changes
  disabled. They left two synthetic test customers and subscriptions in the
  development database.
- Customer welcome/plans, operator index, and Jaeger returned HTTP 200.
  Browser visual inspection was unavailable in this session.
- Subsequent OpenRouter CLI and operator web-chat read-only tool calls passed;
  see the public integration guide for provider verification details.
