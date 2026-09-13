# Public demo integrations

Start with [local development](LOCAL-DEVELOPMENT.md). The public overlay adds a
Caddy gateway and a Cloudflare Tunnel connector to the same Compose project.
The supplied hostname is `demo.bss-cli.com`; set `BSS_PORTAL_PUBLIC_URL` in
`.env` when deploying under another domain.

## Public routing

Create a separate Cloudflare Tunnel for this deployment. Keep any existing
website/tunnel intact. Configure its published application route:

- Hostname: `demo.bss-cli.com` (or your chosen hostname)
- Path: blank
- Service type: HTTP
- Service URL: `demo-gateway:8000`

Save only the connector token in `.secrets/cloudflare-tunnel-token`, with the
`.secrets` directory mode 0700 and token file mode 0600. Use hidden input or a
local secret manager; do not put the token in shell history. The token is mounted
read-only in cloudflared. Both `.env` and `.secrets/` are excluded from Git and
Docker build contexts. Compose uses the invoking user's UID/GID for the connector.

The gateway exposes only the customer portal and provider callbacks:

- `/webhooks/stripe` → `payment:8000`
- All other paths, including Didit/Resend callbacks → `portal-self-serve:8000`

The operator cockpit, database, RabbitMQ, and Jaeger remain bound to loopback.
The gateway's loopback diagnostic port is 9080. HTTPS terminates at Cloudflare;
the portal uses the public HTTPS URL and Secure cookies.

Use the public wrapper for subsequent Compose operations:

```bash
scripts/demo up -d --wait
scripts/demo ps
scripts/demo logs --tail 50 cloudflared
scripts/demo build portal-self-serve
scripts/demo up -d --no-deps --no-build --wait portal-self-serve
scripts/dev bss catalog list
```

`docker compose` v2 with `!override` support is required. To disconnect the demo,
use `scripts/demo stop cloudflared`. To resume, run `scripts/demo up -d --wait`.

## Provider configuration

Keep credentials in the ignored mode-0600 `.env` or an equivalent secret store.
Use a local editor or hidden-input helper; never paste credentials into chat,
commit them, or expose them in diagnostic output. Temporary credential helpers
used during development are intentionally not shipped.

### Resend

Set `BSS_PORTAL_EMAIL_PROVIDER=resend`, the verified sender in
`BSS_PORTAL_EMAIL_FROM`, and `BSS_PORTAL_EMAIL_RESEND_API_KEY`. A verified sending
subdomain must also appear in the sender address (for example,
`noreply@mail.example.com`). Register `https://<hostname>/webhooks/resend` for
`email.sent`, `email.delivered`, `email.delivery_delayed`, `email.bounced`,
`email.complained`, and `email.failed`; save its signing secret in
`BSS_PORTAL_EMAIL_RESEND_WEBHOOK_SECRET`.

The receiver verifies the raw-body Svix signature, redacts recipient fields,
records events idempotently, and returns 503 on storage failure so delivery can
retry. A delivered event confirms mail-server acceptance, not inbox placement.

### Didit V3

Set `BSS_PORTAL_KYC_PROVIDER=didit`, `BSS_PORTAL_KYC_DIDIT_API_KEY`,
`BSS_PORTAL_KYC_DIDIT_WORKFLOW_ID`, and `BSS_PORTAL_KYC_DIDIT_WEBHOOK_SECRET`.
Use a published KYC workflow with identity-document verification. Register a
V3 destination at `https://<hostname>/webhooks/didit` for `status.updated` and
`data.updated`.

The adapter uses V3 session/decision endpoints. Attestation requires an Approved
signed callback and API decision with exactly one complete approved identity
document. Workflows producing wallet-only evidence or multiple approved identity
documents are not supported by this reduction path.

Verification supports canonical Unicode JSON (`X-Signature-V2`) with a full
raw-body (`X-Signature`) fallback. It never accepts `X-Signature-Simple` alone.
Only the event envelope and digest are stored. Event and corroboration writes
commit together; failures are retryable. Deliveries without an event ID use a
content hash excluding the dispatch timestamp, preserving status progression
while deduplicating restamped retries.

### Stripe test mode

Set `BSS_PAYMENT_PROVIDER=stripe`, `BSS_PAYMENT_STRIPE_API_KEY`,
`BSS_PAYMENT_STRIPE_PUBLISHABLE_KEY`, and `BSS_PAYMENT_STRIPE_WEBHOOK_SECRET`.
Use keys from the same test mode/sandbox as the webhook. A restricted test key
(`rk_test_`) needs Write permissions for Customers, Payment Methods, Payment
Intents, Checkout Sessions, and Setup Intents. The publishable key starts with
`pk_test_`; the endpoint signing secret starts with `whsec_`.

Register `https://<hostname>/webhooks/stripe` for `charge.succeeded`,
`charge.failed`, `payment_intent.payment_failed`, `charge.refunded`, and
`charge.dispute.created`. Reload both payment and self-serve after saving keys:

```bash
scripts/demo up -d --no-deps --no-build --wait payment portal-self-serve
```

Existing mock payment methods remain mock tokens after switching providers.
Use a fresh signup and Stripe-hosted test-card setup. This guide does not enable
live charges. Restricted keys preserve the existing test/live mismatch,
production-test-key, and live-card-reuse startup guards.

### OpenRouter

Set `BSS_LLM_API_KEY`, `BSS_LLM_MODEL`, and `BSS_LLM_BASE_URL`. The supplied
Compose configuration selects `https://openrouter.ai/api/v1` and the application
default is `deepseek/deepseek-v4-pro`. Reload both portals after saving the key:

```bash
scripts/demo up -d --no-deps --no-build --wait portal-self-serve portal-csr
scripts/dev bss ask 'Use the catalog read tools to list prepaid plans and prices.'
```

Customer chat defaults to 20 turns/customer/hour and 60 turns/IP/hour, plus a
200-cent/customer/month estimated-cost limit. These application-side limits do
not impose a global OpenRouter account budget.

## Verification and operational limits

Validated on 2026-09-13: local backend/portal scenarios; Resend signed sent and
delivered callbacks; Didit hosted approval; Stripe test payment and signed charge
callback; completed BSS order and active subscription; OpenRouter CLI and operator
web-chat catalog tool calls. Synthetic valid/duplicate/tampered webhook probes
and database persistence/privacy regression tests also passed. Authenticated
customer-chat browser interaction and a full host reboot test remain unverified.

PostgreSQL and RabbitMQ use persistent Docker volumes; credentials and operator
settings are persistent local files. Jaeger traces are memory-only. All long-running
containers in the development/public overlays use `restart: unless-stopped`.
Enable Docker at boot on the host. Deliberately stopped containers stay stopped;
removing containers requires Compose up to recreate them. Do not use `down -v`
when retaining data.

The tunnel uses outbound connections rather than a fixed host IP. Network changes
should reconnect without DNS changes if outbound tunnel access is allowed, but
in-flight requests can fail. Host sleep, power loss, and Internet loss interrupt
availability. Docker daemon restart does not run Compose's health-based startup
ordering; verify dependencies reconnect, and use `scripts/demo up -d --wait` if
manual recovery is necessary.

This is a single-host demo, without second-host failover, off-host backups, or
external uptime alerts. Provider callbacks may require replay after an outage
exceeds their retry windows. Test reboot recovery and backup restoration before
relying on the deployment for unattended operation.
