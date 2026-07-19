#!/usr/bin/env bash
# Rust doctrine-check (Phase 8). The greppable doctrine guards, re-expressed over the
# Rust tree (rust/) — the counterpart to the Python `make doctrine-check` in the
# Makefile. Dispositions per phases/2.0/02-TECH-MAPPING.md §4:
#   - "structural" guards are enforced by construction (workspace deps, newtypes,
#     #![forbid(unsafe_code)], clippy unwrap/expect deny) — noted, not grepped here.
#   - "test" guards are pinned by unit tests (knowledge profile, INDEXED_PATHS,
#     ported_out, routes_crm confirm-gate) — run by `cargo test`, not here.
#   - the grep guards below mirror their Python siblings 1:1 (same intent, Rust paths).
#
# Exit non-zero on the first violated guard. Run from repo root: bash scripts/rust_doctrine_check.sh
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
RUST=rust
fail=0

# guard <name> <hits>  — <hits> non-empty ⇒ violation.
guard() {
  local name="$1" hits="$2"
  if [ -n "$hits" ]; then
    printf '✗ %s\n%s\n' "$name" "$hits"
    fail=1
  else
    printf '✓ %s\n' "$name"
  fi
}

# 1 — clock: business logic (services/*/src) routes wall-clock through
# bss_clock::now(), never Utc/Local/SystemTime::now directly. `Instant::now()`
# (monotonic, latency metrics) is not matched. Exempt infra/display sites that must
# read *real* wall time (not the deterministic scenario clock), mirroring the Python
# guard's `# noqa: bss-clock` allowlist:
#   - bss-clock impl + tests;
#   - the cockpit renderer wall-clock fallback (display default);
#   - bss-webhooks signature timestamp tolerance (must be real time or sigs break);
#   - bss-portal-auth email_change token-expiry stamps;
#   - the bss-clients default-timestamp fallback;
#   - the portals' static-asset cache-bust version (build-time, not business time).
guard "clock: wall-clock via bss_clock::now (no direct Utc/Local/SystemTime::now)" \
  "$(grep -rnE '(Utc|Local)::now\(\)|SystemTime::now\(\)' --include='*.rs' \
      "$RUST/crates" "$RUST/services" "$RUST/portals" "$RUST/cli" 2>/dev/null \
      | grep -v '/bss-clock/' \
      | grep -v '/tests/' \
      | grep -v '/bss-cockpit/src/renderers/' \
      | grep -v '/bss-webhooks/src/signatures\.rs:' \
      | grep -v '/bss-portal-auth/src/email_change\.rs:' \
      | grep -v '/bss-clients/src/crm\.rs:' \
      | grep -vE '/portals/[^/]+/src/templating\.rs:' \
      | grep -v '// allow: bss-clock' || true)"

# 3 — OTel API stays in the platform crates (telemetry/middleware/events/context);
# services/portals/cli never import opentelemetry directly (structural + grep).
guard "otel: opentelemetry stays in platform crates" \
  "$(grep -rnE 'use opentelemetry|opentelemetry::' --include='*.rs' \
      "$RUST/services" "$RUST/portals" "$RUST/cli" 2>/dev/null \
      | grep -vE '/(bss-telemetry|bss-middleware|bss-events|bss-context)/' || true)"

# 4 — no campaignos leakage.
guard "campaignos: untouched" \
  "$(grep -rn 'campaignos' "$RUST" 2>/dev/null \
      | grep -v '/target/' \
      | grep -v '/tests/' \
      | grep -v '// noqa: campaignos' || true)"

# 5 — renewal reads the price snapshot, never the active-catalog lookup. The renew()
# body (service.rs 737..schedule_plan_change) must not call get_active_price (that
# lives in policies.rs for plan-change only).
guard "renewal: renew() reads snapshot, not catalog active-price" \
  "$(awk '/pub async fn renew\(/,/pub async fn schedule_plan_change\(/' \
      "$RUST/services/subscription/src/service.rs" 2>/dev/null \
      | grep -nE 'get_active_price' || true)"

# 6 — service_identity comes from TokenMap validation, never a caller header.
guard "identity: no caller-asserted X-BSS-Service-Identity header" \
  "$(grep -rn 'X-BSS-Service-Identity' --include='*.rs' \
      "$RUST/crates" "$RUST/services" "$RUST/portals" "$RUST/cli" 2>/dev/null \
      | grep -v '/tests/' || true)"

# 7 — API tokens load once at app/client construction, never per-request. std::env
# reads of a *_API_TOKEN are confined to the config layer + main bootstrap + portal
# app-assembly (lib.rs) + the one-shot CLI commands (no request loop) + the clients
# auth provider. A read inside a route handler would be a per-request env read.
guard "tokens: *_API_TOKEN read once at startup, not per-request" \
  "$(grep -rnE 'env::var\(("|.)BSS_[A-Z_]*API_TOKEN' --include='*.rs' \
      "$RUST/crates" "$RUST/services" "$RUST/portals" "$RUST/cli" 2>/dev/null \
      | grep -vE '/(config|main|lib)\.rs:' \
      | grep -v '/bss-clients/' \
      | grep -v '/cli/' \
      | grep -v '/tests/' \
      | grep -v '// allow: token-runtime-read' || true)"

# 9 — the orchestrator streaming entrypoint (astream_once*) stays in the chat routes
# only; signup + post-login self-serve + CSR CRM screens write direct via bss-clients.
# Match the call form `astream_once…(` so a doc-comment mention doesn't trip it.
guard "astream_once: chat routes only (self-serve chat.rs / csr cockpit.rs)" \
  "$(grep -rnE 'astream_once[a-z_]*\(' --include='*.rs' "$RUST/portals" 2>/dev/null \
      | grep -vE '/(chat|cockpit)\.rs:' \
      | grep -v '/tests/' || true)"

# 11 — the pure rating function stays roaming-unaware. rate_usage() (domain.rs
# 75..currency_of) must not reference data_roaming; roaming routing lives in
# decide_usage_outcome() (the consumer path).
guard "rating: rate_usage() unaware of roaming" \
  "$(awk '/pub fn rate_usage\(/,/^fn currency_of\(/' \
      "$RUST/services/rating/src/domain.rs" 2>/dev/null \
      | grep -nE 'data_roaming' || true)"

# 12 — ported_out is terminal: no code path flips a ported_out MSISDN back to
# 'available' (it is quarantined to 9999-12-31). Pinned by a test too; grep catches
# an accidental UPDATE ... SET status='available' on the ported_out path.
guard "inventory: ported_out is terminal (no available loopback)" \
  "$(grep -rnE "status\s*=\s*'available'[^;]*ported_out|ported_out[^;]*status\s*=\s*'available'" \
      --include='*.rs' "$RUST/services/crm/src" 2>/dev/null || true)"

# 13 — renewal worker is confined to the subscription lifespan tick loop. sweep_due /
# sweep_skipped / the tick loop live in worker.rs and are referenced by main.rs
# (lifespan) + routes.rs (admin tick) only — any other reference is a parallel
# scheduler that breaks the FOR UPDATE SKIP LOCKED multi-replica safety.
guard "renewal-worker: confined to subscription worker.rs + lifespan/admin" \
  "$(grep -rnE 'sweep_due|sweep_skipped|renewal_tick' --include='*.rs' \
      "$RUST/services" "$RUST/crates" "$RUST/portals" "$RUST/cli" 2>/dev/null \
      | grep -vE '/subscription/src/(worker|main|routes|config)\.rs:' \
      | grep -v '/tests/' || true)"

# 14 — service version is sourced from bss_models::BSS_RELEASE, never a hardcoded
# literal in config.rs.
guard "version: sourced from BSS_RELEASE (no hardcoded version literal)" \
  "$(grep -rnE 'version:\s*(String|&str)?\s*=\s*"[0-9]' --include='config.rs' \
      "$RUST/services" "$RUST/portals" 2>/dev/null || true)"

# 17 — the outbox relay is the single MQ publisher. basic_publish is confined to
# bss-events; services stage events and never publish directly.
guard "outbox: basic_publish confined to bss-events" \
  "$(grep -rn 'basic_publish' --include='*.rs' \
      "$RUST/services" "$RUST/portals" "$RUST/cli" 2>/dev/null \
      | grep -v '/bss-events/' || true)"

# 19 — email palette comes from bss_branding::THEMES only; no hex colour literals in
# the email renderer.
guard "branding: no hex literals in the email renderer" \
  "$(grep -nE '#[0-9a-fA-F]{6}\b' "$RUST/crates/bss-portal-auth/src/email.rs" 2>/dev/null || true)"

# 21 — settings.toml writes go through bss-cockpit (toml_edit); bss-branding is the
# read path. No toml_edit:: usage anywhere else.
guard "settings: toml_edit confined to bss-cockpit (+ bss-branding read)" \
  "$(grep -rn 'toml_edit::' --include='*.rs' \
      "$RUST/services" "$RUST/portals" "$RUST/cli" \
      "$RUST/crates" 2>/dev/null \
      | grep -vE '/(bss-cockpit|bss-branding)/' || true)"

echo
if [ "$fail" -ne 0 ]; then
  echo "doctrine-check (rust): FAILED"
  exit 1
fi
echo "doctrine-check (rust): all guards passed"
