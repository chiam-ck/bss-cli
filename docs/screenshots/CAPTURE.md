# Capturing screenshots for `docs/screenshots/`

> v0.18 baseline. The committed PNGs are what the README links to.
> Naming convention: `<surface>_v0_X.png`. Re-capture when the
> rendered surface meaningfully changes (banner, layout, brand
> elements, version stamp).

## Web surfaces (Playwright)

```bash
# Bring the stack up
docker compose up -d --wait

# Dev-only deps (not in the workspace lock)
uv pip install playwright
uv run python -m playwright install chromium

# Capture
uv run python docs/screenshots/capture_portals.py
```

The script captures four surfaces and writes them next to itself:

| File | URL | Notes |
|---|---|---|
| `portal_self_serve_welcome_v0_18.png` | `localhost:9001/welcome` | Public landing — brand bar + Sign in / Browse plans CTAs |
| `portal_self_serve_plans_v0_18.png` | `localhost:9001/plans` | Three-card plan picker; v0.17 Roaming row visible (PLAN_S "—", PLAN_M 500 mb, PLAN_L 2 GB) |
| `portal_csr_cockpit_sessions_v0_18.png` | `localhost:9002/` | Cockpit sessions index — "Hello, operator", recent conversations |
| `portal_csr_cockpit_session_v0_18.png` | `localhost:9002/cockpit/SES-...` | Live cockpit conversation — chosen at runtime as the session with the most messages |

Re-run any time the running stack reflects the surface you want.
The captures use a 1280×800 viewport, dark color scheme, headless
chromium from `~/.cache/ms-playwright` (override via
`PLAYWRIGHT_CHROMIUM_EXECUTABLE`).

## Promo-code self-serve surfaces (v1.1)

`capture_promo.py` shoots two authenticated self-serve surfaces that show
the promo flow: the signup form's live discounted-price preview, and the
dashboard line card with the applied discount. Both are behind the
verified-email / session wall, and the signup funnel ends in KYC + payment,
so the **stack must be in mock/dev provider mode** for the headless run
(real Resend/Didit/Stripe can't be driven by Playwright).

```bash
# Temporarily flip 3 containers to mock providers (no .env edit), via an
# override that sets: BSS_PORTAL_EMAIL_PROVIDER=logging (OTP → dev mailbox),
# BSS_PORTAL_KYC_PROVIDER=prebaked, BSS_PAYMENT_PROVIDER=mock,
# BSS_ENABLE_TEST_ENDPOINTS=true. In mock mode the funnel auto-advances
# (prebaked KYC is synchronous; mock COF/order/poll fire on hx-trigger=load).
docker compose -f docker-compose.yml -f docker-compose.screenshots.yml \
  up -d portal-self-serve payment crm

# A multi-use public promo the demo customer can redeem cleanly:
uv run bss promo create --id PROMO_DEMO15 --type percent --value 15 \
  --duration multi --periods 3 --audience public --code DEMO15 \
  --code-kind multi_use --name "Launch 15%"

# Capture (pass a fresh available MSISDN; SOM doesn't fall back if the
# preferred number is already reserved):
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/snap/bin/chromium \
  PROMO_MSISDN=<fresh> PROMO_CODE=DEMO15 \
  uv run python docs/screenshots/capture_promo.py

# Restore real providers:
make up
```

Writes `portal_self_serve_signup_promo_v1_1.png` and
`portal_self_serve_dashboard_promo_v1_1.png`. On this host Playwright
can't download its own chromium (unsupported platform); point
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` at a system chromium (snap chromium needs
the `--no-sandbox` args the script already passes).

## Trace swimlane (terminal `bss trace`)

```bash
uv run python docs/screenshots/capture_trace.py
```

Produces `bss_trace_swimlane_v0_2.png`. Hasn't changed since v0.2;
re-capture if the renderer meaningfully evolves.

## Terminal REPL banner (`bss_repl_v0_19.jpg`)

The Rich-rendered REPL banner can't be captured by Playwright (no
DOM) and headless terminal capture loses the ANSI rendering. Capture
this manually:

1. Open a wide terminal (~2000×1300 px — ghostty / kitty / iterm2 /
   alacritty all work).
2. `uv run bss`.
3. Run a few representative queries so the banner sits above real
   conversation output:
   ```
   list all products
   how about the VASes?
   show me more details about PLAN_L please
   ```
4. Take a window screenshot and save as
   `docs/screenshots/bss_repl_v0_19.jpg` (PNG also fine — terminals
   produce either; the v0.19 capture is JPEG @ 3200×2092 ~510 KB
   which renders crisply on github.com).

The README links this filename verbatim; commit at the same path.

## Discipline

- **No real customer data.** Captures use scenario-fixture names
  (`Trace Demo` / `Ck Demo` / `portal-demo-*`).
- **Dark theme only.**
- **Commit the PNGs.** `docs/screenshots/*.png` are part of the repo,
  not external links.
- **Optimize.** If `oxipng` is on PATH, the capture script runs
  `oxipng -o 4` automatically; install it for a 30-50% size win
  (`apt install oxipng`).
